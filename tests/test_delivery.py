"""Отправка: сборка MIME и защита канала до принимающего сервера.

Отдельный от воркера слой: здесь только транспорт, очередь и повторы — в
`test_workers.py`.
"""

import ssl
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.constants import Direction, MessageStatus, OutboundMode
from app.models import Attachment, Message
from app.services import delivery
from app.services.delivery import PermanentSendError


class FakeSMTP:
    """Подставной сервер: запоминает, как с ним разговаривали."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, local_hostname=None, timeout=None):
        self.host = host
        self.port = port
        self.local_hostname = local_hostname
        self.supports_starttls = True
        self.tls_context = None
        self.credentials = None
        self.sent = None
        self.sock = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        pass

    def has_extn(self, _name):
        return self.supports_starttls

    def starttls(self, context=None):
        self.tls_context = context
        # Настоящий starttls подменяет сокет на SSL-обёртку; проверка
        # шифрования смотрит именно на его тип.
        self.sock = MagicMock(spec=ssl.SSLSocket)

    def login(self, username, password):
        self.credentials = (username, password)

    def sendmail(self, sender, recipients, raw):
        # Именно sendmail с готовыми байтами, а не send_message: письмо
        # подписано DKIM, и повторная сборка сломала бы подпись.
        self.sent = (sender, recipients, raw)
        return {}


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


@pytest.fixture
def relay_mode(monkeypatch):
    monkeypatch.setattr(settings, "outbound_mode", OutboundMode.RELAY)
    monkeypatch.setattr(settings, "relay_host", "relay.example.kz")
    monkeypatch.setattr(settings, "relay_port", 587)
    monkeypatch.setattr(settings, "relay_username", "gateway")
    monkeypatch.setattr(settings, "relay_password", "секрет")
    monkeypatch.setattr(settings, "relay_use_tls", True)
    monkeypatch.setattr(settings, "relay_verify_tls", True)
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSMTP)


def outgoing(**overrides) -> Message:
    fields = {
        "direction": Direction.OUTGOING,
        "status": MessageStatus.QUEUED,
        "sender": "sales@test-gw.kz",
        "recipient": "client@outside.example",
        "subject": "Тема",
        "body_text": "Текст",
        "message_id": "<out-1@test-gw.kz>",
    }
    return Message(**{**fields, **overrides})


# --- Защита канала до релея --------------------------------------------------- #


def test_relay_tls_is_verified():
    """По этому каналу уходит пароль релея — сертификат обязан проверяться."""
    context = delivery._relay_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_relay_verification_can_be_waived_deliberately(monkeypatch):
    """Послабление под внутренний самоподписанный релей — только явное."""
    monkeypatch.setattr(settings, "relay_verify_tls", False)
    context = delivery._relay_context()
    assert context.verify_mode == ssl.CERT_NONE


def test_relay_delivery_passes_verifying_context(relay_mode):
    """Регрессия: без явного контекста smtplib берёт непроверяющий.

    Молчаливое поведение библиотеки — не то, на что можно опираться в канале
    с учётными данными.
    """
    delivery.deliver(outgoing(), None)

    server = FakeSMTP.instances[0]
    assert server.tls_context is not None
    assert server.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert server.credentials == ("gateway", "секрет")


def test_relay_without_tls_never_sends_credentials(relay_mode, monkeypatch):
    """Сервер молчит про STARTTLS, а TLS затребован — пароль не отдаём."""
    def no_tls(*args, **kwargs):
        server = FakeSMTP(*args, **kwargs)
        server.supports_starttls = False
        return server

    monkeypatch.setattr(delivery.smtplib, "SMTP", no_tls)
    with pytest.raises(PermanentSendError):
        delivery.deliver(outgoing(), None)

    assert FakeSMTP.instances[0].credentials is None
    assert FakeSMTP.instances[0].sent is None


# --- Оппортунистический TLS до чужого MX -------------------------------------- #


def test_mx_tls_is_deliberately_unverified():
    """RFC 7435: у публичных MX сертификаты сплошь самоподписанные.

    Требование проверки означало бы не «безопаснее», а «письмо не ушло»:
    альтернатива на 25-м порту — открытый текст, а не проверенный канал.
    """
    context = delivery._mx_context()
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_direct_delivery_still_passes_explicit_context(monkeypatch):
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(delivery, "_mx_hosts", lambda _domain: ["mx.outside.example"])

    raw = delivery.serialize(delivery.build_mime(outgoing()), None)
    delivery._deliver_direct(raw, "sales@test-gw.kz", "client@outside.example")

    assert FakeSMTP.instances[0].tls_context is not None


# --- Сборка письма ------------------------------------------------------------- #


def test_mime_carries_headers_and_both_bodies():
    msg = delivery.build_mime(outgoing(body_html="<p>Текст</p>"))

    assert msg["From"] == "sales@test-gw.kz"
    assert msg["To"] == "client@outside.example"
    assert msg["Message-ID"] == "<out-1@test-gw.kz>"
    assert msg["Date"]  # без даты письмо режут спам-фильтры
    assert msg.get_body(preferencelist=("plain",)) is not None
    assert msg.get_body(preferencelist=("html",)) is not None


def test_reply_headers_link_the_thread():
    msg = delivery.build_mime(
        outgoing(in_reply_to="<parent@example.org>", thread_key="<root@example.org>")
    )
    assert msg["In-Reply-To"] == "<parent@example.org>"
    assert msg["References"] == "<root@example.org>"


def test_missing_attachment_file_does_not_block_the_letter(tmp_path):
    """Файл мог быть удалён ротацией — письмо всё равно должно уйти."""
    message = outgoing()
    message.attachments.append(
        Attachment(filename="нет.pdf", filepath=str(tmp_path / "нет.pdf"),
                   content_type="application/pdf", size=10)
    )
    msg = delivery.build_mime(message)
    assert msg["Subject"] == "Тема"
    assert list(msg.iter_attachments()) == []
