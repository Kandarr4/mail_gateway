"""Безопасность приёма: подлинность отправителя, TLS, лимиты SMTP."""

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.constants import AuthResult, InboundAuthPolicy
from app.services import inbound_auth, receiving
from app.services.inbound_auth import UNCHECKED, AuthVerdict
from app.services.receiving import Outcome
from tests.conftest import make_mailbox

MAILBOX = "sales@test-gw.kz"


@pytest.fixture
def mailbox(db):
    make_mailbox(db, address=MAILBOX, is_active=True)
    db.commit()


def letter(message_id="<sec-1@outside.example>") -> bytes:
    msg = EmailMessage()
    msg["From"] = "client@outside.example"
    msg["To"] = MAILBOX
    msg["Subject"] = "Проверка"
    msg["Message-ID"] = message_id
    msg.set_content("тело")
    return msg.as_bytes()


# --- Разбор ответа проверяющей библиотеки ----------------------------------- #


def test_parses_all_three_methods():
    header = ('Authentication-Results: mail.test-gw.kz; spf=pass smtp.mailfrom=a@b.kz; '
              'dkim=fail; dmarc=fail (Used From Domain Record) policy.dmarc=reject')
    verdict = inbound_auth._parse(header)

    assert verdict.spf == AuthResult.PASS
    assert verdict.dkim == AuthResult.FAIL
    assert verdict.dmarc == AuthResult.FAIL
    assert verdict.policy == "reject"


def test_missing_methods_default_to_none():
    verdict = inbound_auth._parse("Authentication-Results: mail.test-gw.kz; spf=pass")
    assert verdict.dkim == AuthResult.NONE
    assert verdict.dmarc == AuthResult.NONE


@pytest.mark.parametrize(
    ("dmarc", "policy", "expected"),
    [
        (AuthResult.FAIL, "reject", True),
        (AuthResult.FAIL, "quarantine", False),  # домен просит решать самим
        (AuthResult.FAIL, "none", False),
        (AuthResult.PASS, "reject", False),
        (AuthResult.NONE, "", False),
    ],
)
def test_reject_only_when_domain_demands_it(dmarc, policy, expected):
    """Своей политики поверх чужой не выдумываем: `p=reject` — явное указание."""
    assert AuthVerdict(dmarc=dmarc, policy=policy).should_reject is expected


def test_verification_failure_does_not_lose_the_letter(monkeypatch):
    """Недоступный DNS не должен приводить к потере письма."""
    monkeypatch.setattr("app.config.settings.inbound_auth", InboundAuthPolicy.ANNOTATE)

    def boom(*_args, **_kwargs):
        raise OSError("DNS недоступен")

    monkeypatch.setattr("authheaders.authenticate_message", boom)
    assert inbound_auth.verify(letter(), peer_ip="8.8.8.8", mail_from="a@b.kz", helo="b.kz") is UNCHECKED


def test_dmarc_lookup_survives_a_non_utf8_locale():
    """Проверка подлинности не должна зависеть от кодировки системы.

    `authheaders` открывает список публичных суффиксов без указания кодировки,
    и на русской Windows `open()` берёт cp1251. Список — UTF-8, чтение падает,
    разбор DMARC срывается, а защитный `except` в `verify()` превращает это в
    молчаливый приём непроверенных писем: в журнале одна строка, проверки не
    работают, и заметить это можно только специально.

    Подпроцесс с выключенным режимом UTF-8 воспроизводит ровно ту среду.
    """
    import subprocess
    import sys

    code = (
        "from app.services.inbound_auth import _force_utf8_public_suffix_list;"
        "_force_utf8_public_suffix_list();"
        "from authheaders.dmarc_lookup import get_org_domain;"
        "print(get_org_domain('mail.gmail.com'))"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", code],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "gmail.com"


def test_no_peer_ip_skips_check(monkeypatch):
    """Без адреса подключившегося SPF проверять не по чему (шлюз за прокси)."""
    monkeypatch.setattr("app.config.settings.inbound_auth", InboundAuthPolicy.ANNOTATE)
    assert inbound_auth.verify(letter(), peer_ip=None, mail_from="a@b.kz", helo="b.kz") is UNCHECKED


def test_policy_off_skips_check(monkeypatch):
    monkeypatch.setattr("app.config.settings.inbound_auth", InboundAuthPolicy.OFF)
    assert inbound_auth.verify(letter(), peer_ip="8.8.8.8", mail_from="a@b.kz", helo="b.kz") is UNCHECKED


# --- Влияние вердикта на приём ---------------------------------------------- #


def _with_verdict(monkeypatch, verdict: AuthVerdict, policy: InboundAuthPolicy):
    monkeypatch.setattr("app.config.settings.inbound_auth", policy)
    monkeypatch.setattr(inbound_auth, "verify", lambda *a, **k: verdict)


def test_annotate_accepts_and_records_failure(mailbox, db, monkeypatch):
    """Режим annotate ничего не отбивает — только фиксирует результат."""
    failed = AuthVerdict(spf=AuthResult.FAIL, dkim=AuthResult.FAIL, dmarc=AuthResult.FAIL,
                         policy="reject", header="Authentication-Results: ...")
    _with_verdict(monkeypatch, failed, InboundAuthPolicy.ANNOTATE)

    result = receiving.accept(letter(), [MAILBOX], "client@outside.example", peer_ip="8.8.8.8")

    assert result.outcome is Outcome.STORED
    from app.models import Message

    message = db.get(Message, result.message_id)
    assert message.spf_result == AuthResult.FAIL
    assert message.dmarc_result == AuthResult.FAIL
    assert message.auth_results


def test_enforce_rejects_when_domain_demands(mailbox, monkeypatch):
    failed = AuthVerdict(dmarc=AuthResult.FAIL, policy="reject")
    _with_verdict(monkeypatch, failed, InboundAuthPolicy.ENFORCE)

    result = receiving.accept(letter(), [MAILBOX], "client@outside.example", peer_ip="8.8.8.8")
    assert result.outcome is Outcome.NOT_AUTHENTIC


def test_enforce_accepts_quarantine_policy(mailbox, monkeypatch):
    """p=quarantine означает «решай сам» — принимаем, но с пометкой."""
    verdict = AuthVerdict(dmarc=AuthResult.FAIL, policy="quarantine")
    _with_verdict(monkeypatch, verdict, InboundAuthPolicy.ENFORCE)

    result = receiving.accept(letter(), [MAILBOX], "client@outside.example", peer_ip="8.8.8.8")
    assert result.outcome is Outcome.STORED


def test_passing_letter_is_stored_with_results(mailbox, db, monkeypatch):
    good = AuthVerdict(spf=AuthResult.PASS, dkim=AuthResult.PASS, dmarc=AuthResult.PASS)
    _with_verdict(monkeypatch, good, InboundAuthPolicy.ENFORCE)

    result = receiving.accept(letter(), [MAILBOX], "client@outside.example", peer_ip="8.8.8.8")

    from app.models import Message

    assert db.get(Message, result.message_id).dmarc_result == AuthResult.PASS


def test_results_are_visible_through_api(mailbox, db, client, monkeypatch):
    """Решение о доверии принимает потребитель — значит, результат должен быть виден."""
    from tests.conftest import AUTH

    _with_verdict(monkeypatch, AuthVerdict(spf=AuthResult.FAIL, dmarc=AuthResult.FAIL),
                  InboundAuthPolicy.ANNOTATE)
    receiving.accept(letter(), [MAILBOX], "client@outside.example", peer_ip="8.8.8.8")

    page = client.get("/api/v1/messages", headers=AUTH, params={"dmarc_result": "fail"}).json()
    assert page["total"] == 1
    assert page["items"][0]["spf_result"] == "fail"


# --- Адрес отправителя за прокси -------------------------------------------- #


class _FakeProxyData:
    src_addr = "203.0.113.7"


class _FakeSession:
    def __init__(self, peer=("192.168.50.1", 51234), proxy_data=None):
        self.peer = peer
        self.proxy_data = proxy_data


def test_client_ip_from_socket_without_proxy(monkeypatch):
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.proxy_protocol", False)
    assert smtp_server.client_ip(_FakeSession()) == "192.168.50.1"


def test_client_ip_from_proxy_header(monkeypatch):
    """За прокси `session.peer` — это прокси; SPF по нему проверять бессмысленно."""
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.proxy_protocol", True)
    session = _FakeSession(proxy_data=_FakeProxyData())
    assert smtp_server.client_ip(session) == "203.0.113.7"


def test_proxy_header_ignored_when_disabled(monkeypatch):
    """Доверять заголовку без явного разрешения нельзя — его подделает кто угодно."""
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.proxy_protocol", False)
    session = _FakeSession(proxy_data=_FakeProxyData())
    assert smtp_server.client_ip(session) == "192.168.50.1"


# --- TLS и лимиты ------------------------------------------------------------ #


def test_no_tls_context_without_cert(monkeypatch):
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.tls_cert_file", "")
    assert smtp_server.tls_context() is None


def test_missing_cert_file_is_reported(monkeypatch, tmp_path):
    """Молча стартовать без шифрования, когда его явно просили, нельзя."""
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.tls_cert_file", str(tmp_path / "нет.pem"))
    monkeypatch.setattr("app.config.settings.tls_key_file", str(tmp_path / "нет.key"))
    monkeypatch.setattr(type(smtp_server.settings), "tls_paths",
                        property(lambda self: (tmp_path / "нет.pem", tmp_path / "нет.key")))

    with pytest.raises(FileNotFoundError, match="сертификата"):
        smtp_server.tls_context()


def test_starttls_is_advertised_and_works(tmp_path, monkeypatch):
    """Проверка сквозная: реальный сертификат, реальный STARTTLS."""
    cert, key = _self_signed(tmp_path)
    from app import smtp_server

    monkeypatch.setattr(type(smtp_server.settings), "tls_paths", property(lambda self: (cert, key)))
    monkeypatch.setattr("app.config.settings.smtp_host", "127.0.0.1")
    monkeypatch.setattr("app.config.settings.smtp_port", 8825)

    controller = smtp_server.start_smtp()
    try:
        with smtplib.SMTP("127.0.0.1", 8825, timeout=10) as client:
            client.ehlo()
            assert client.has_extn("starttls"), "STARTTLS не объявлен"
            assert client.has_extn("smtputf8"), "SMTPUTF8 не объявлен — адреса с кириллицей отпадут"

            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            code, _ = client.starttls(context=context)
            assert code == 220
    finally:
        controller.stop()


def test_greeting_uses_fully_qualified_name(monkeypatch):
    """В приветствии должно стоять доменное имя, а не имя машины."""
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.smtp_host", "127.0.0.1")
    monkeypatch.setattr("app.config.settings.smtp_port", 8827)
    monkeypatch.setattr(type(smtp_server.settings), "tls_paths", property(lambda self: None))
    monkeypatch.setattr(type(smtp_server.settings), "smtp_name",
                        property(lambda self: "mail.test-gw.kz"))

    controller = smtp_server.start_smtp()
    try:
        with smtplib.SMTP("127.0.0.1", 8827, timeout=10) as client:
            client.ehlo("check.local")
            greeting = client.ehlo_resp.splitlines()[0].decode()
            assert greeting == "mail.test-gw.kz"
    finally:
        controller.stop()


def test_message_size_limit_is_advertised(monkeypatch):
    from app import smtp_server

    monkeypatch.setattr("app.config.settings.smtp_host", "127.0.0.1")
    monkeypatch.setattr("app.config.settings.smtp_port", 8826)
    monkeypatch.setattr(type(smtp_server.settings), "tls_paths", property(lambda self: None))

    controller = smtp_server.start_smtp()
    try:
        with smtplib.SMTP("127.0.0.1", 8826, timeout=10) as client:
            client.ehlo()
            assert client.has_extn("size")
            assert int(client.esmtp_features["size"]) == smtp_server.settings.max_message_bytes
    finally:
        controller.stop()


def _self_signed(tmp_path):
    """Самоподписанный сертификат для проверки STARTTLS."""
    pytest.importorskip("cryptography")
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.test-gw.kz")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path
