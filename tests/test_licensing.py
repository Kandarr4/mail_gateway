"""Лицензирование: подпись, сроки, отказ обрабатывать почту без лицензии.

Две гарантии, и обе проверяются здесь. Первая: недействительный файл
закрывает почту — каждым способом обойтись без лицензии (нет файла, подделка,
чужая программа, переведённые часы). Вторая, обратная: всё остальное при этом
работает — служба поднимается, панель доступна, письма ждут в очереди, а не
пропадают.
"""

import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services import clients, domains, licensing
from tests.conftest import AUTH, make_license


@pytest.fixture(autouse=True)
def _clean_license_cache():
    """Тесты подменяют файл лицензии — кеш статуса не должен переживать их.

    monkeypatch вернёт LICENSE_FILE на место, а кеш остался бы с чужим
    ответом и сломал бы следующий тест.
    """
    licensing.reset_cache()
    yield
    licensing.reset_cache()


# --- Проверка файла -------------------------------------------------------------- #


def test_valid_license_verifies(tmp_path):
    path = tmp_path / "license.lic"
    make_license(path)
    result = licensing.verify_file(path)
    assert result.valid, result.reason
    assert result.company == "ТОО Тест"
    assert result.max_domains == 3


def test_expired_license_is_invalid(tmp_path):
    path = tmp_path / "license.lic"
    make_license(path, days=10, issue_shift_days=-20)
    result = licensing.verify_file(path)
    assert not result.valid
    assert "истёк" in result.reason


def test_foreign_signature_is_rejected(tmp_path):
    """Файл, подписанный чужим ключом, — не лицензия."""
    path = tmp_path / "license.lic"
    make_license(path, key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    result = licensing.verify_file(path)
    assert not result.valid
    assert "Подпись" in result.reason


def test_tampered_payload_is_rejected(tmp_path):
    """Правка данных (например, max_domains) ломает подпись."""
    path = tmp_path / "license.lic"
    make_license(path, max_domains=3)
    decoded = base64.b64decode(path.read_bytes())
    tampered = decoded.replace(b'"max_domains": 3', b'"max_domains": 9')
    path.write_bytes(base64.b64encode(tampered))

    assert not licensing.verify_file(path).valid


def test_other_programs_license_is_rejected(tmp_path):
    path = tmp_path / "license.lic"
    make_license(path, program_id="quick-queue", max_operators=5)
    result = licensing.verify_file(path)
    assert not result.valid
    assert "другой программы" in result.reason


def test_clock_rollback_does_not_extend(tmp_path):
    """Перевод системных часов назад не «продлевает» лицензию."""
    path = tmp_path / "license.lic"
    make_license(path, issue_shift_days=+10)
    result = licensing.verify_file(path)
    assert not result.valid
    assert "раньше даты выдачи" in result.reason


def test_garbage_file_is_reported_not_crashed(tmp_path):
    path = tmp_path / "license.lic"
    path.write_bytes(b"\x00\x01 not a license")
    assert not licensing.verify_file(path).valid


def test_missing_file_is_reported(tmp_path):
    assert not licensing.verify_file(tmp_path / "нет.lic").valid


# --- Почта без лицензии не ходит, а программа работает ------------------------------- #


@pytest.mark.parametrize(
    ("case", "make"),
    [
        ("нет файла", lambda path: None),
        ("просрочена", lambda path: make_license(path, days=10, issue_shift_days=-20)),
        ("мусор", lambda path: path.write_bytes(b"not a license")),
        ("чужая подпись", lambda path: make_license(
            path, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )),
        ("другая программа", lambda path: make_license(path, program_id="quick-queue")),
        ("часы переведены назад", lambda path: make_license(path, issue_shift_days=+10)),
    ],
)
def test_mail_forbidden_without_valid_license(case, make, monkeypatch, tmp_path):
    """Ни один способ обойтись без лицензии не открывает почту."""
    path = tmp_path / "license.lic"
    make(path)
    monkeypatch.setattr(licensing, "LICENSE_FILE", path)
    licensing.reset_cache()

    assert not licensing.mail_allowed()
    # Отказ обязан объяснять оператору, куда идти за лицензией.
    assert licensing.LICENSING_URL in licensing.suspension_notice()


def test_mail_allowed_with_valid_license(monkeypatch, tmp_path):
    path = tmp_path / "license.lic"
    make_license(path)
    monkeypatch.setattr(licensing, "LICENSE_FILE", path)
    licensing.reset_cache()

    assert licensing.mail_allowed()


def test_installed_license_opens_mail_without_restart(monkeypatch, tmp_path):
    """Обещание установщика и соглашения: перезапуск не нужен.

    Разрешение спрашивается на каждом письме, поэтому положенный в работающий
    шлюз файл включает почту сам — на это опирается весь сценарий установки
    без лицензии.
    """
    target = tmp_path / "license.lic"
    monkeypatch.setattr(licensing, "LICENSE_FILE", target)
    licensing.reset_cache()
    assert not licensing.mail_allowed()

    fresh = tmp_path / "новая.lic"
    make_license(fresh)
    licensing.install_license_file(fresh)

    assert licensing.mail_allowed()


def test_server_starts_without_license(monkeypatch, tmp_path):
    """Служба обязана подниматься и без лицензии: иначе лицензию некуда
    установить, а оператор видит остановленную службу."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "нет.lic")
    licensing.reset_cache()

    with TestClient(create_app()) as started:
        assert started.get("/api/v1/ready").status_code == 200


def test_smtp_refuses_mail_temporarily_without_license(monkeypatch, tmp_path):
    """Именно временный отказ (4xx): отправитель повторит доставку, и письма
    дойдут после установки лицензии, а не вернутся авторам как несуществующие."""
    import asyncio

    from app.smtp_server import EmailHandler

    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "нет.lic")
    licensing.reset_cache()

    envelope = SimpleNamespace(mail_from=None, mail_options=[], rcpt_tos=[])
    reply = asyncio.run(
        EmailHandler().handle_MAIL(None, SimpleNamespace(peer=("10.0.0.1", 25)),
                                   envelope, "sender@outside.example", [])
    )

    assert reply.startswith("4"), reply
    assert envelope.mail_from is None  # конверт даже не начат


def test_outbox_worker_pauses_without_license(monkeypatch, tmp_path):
    """Письма ждут в очереди, а не помечаются проваленными: чужую почту
    терять из-за нашей коммерции нельзя."""
    from app.workers.outbox import OutboxWorker

    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "нет.lic")
    licensing.reset_cache()
    assert licensing.LICENSING_URL in OutboxWorker().pause_reason()

    make_license(tmp_path / "license.lic")
    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "license.lic")
    licensing.reset_cache()
    assert OutboxWorker().pause_reason() == ""


def test_api_refuses_to_queue_mail_without_license(client, monkeypatch, tmp_path):
    """Молча осевшее в очереди письмо интегрирующая система считает
    отправленным — отказываем сразу и с объяснением."""
    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "нет.lic")
    licensing.reset_cache()

    response = client.post(
        "/api/v1/messages",
        headers=AUTH,
        json={"sender": "a@test-gw.kz", "recipient": "b@outside.example", "body_text": "x"},
    )

    assert response.status_code == 503
    assert licensing.LICENSING_URL in response.json()["detail"]


# --- Лимит доменов действующей лицензии ---------------------------------------------- #


def test_domain_limit_enforced_with_valid_license(db, monkeypatch, tmp_path):
    path = tmp_path / "license.lic"
    make_license(path, max_domains=2)
    monkeypatch.setattr(licensing, "LICENSE_FILE", path)
    licensing.reset_cache()

    # Домен «Default» из бутстрапа уже существует — добавляем второй.
    row = clients.create(db, "Лимитный")
    domains.create(db, row.id, "second.example")
    db.commit()

    allowed, reason = licensing.check_domain_limit(db)
    assert not allowed
    assert "лимит" in reason.lower()


def test_domain_limit_allows_below_maximum(db, monkeypatch, tmp_path):
    path = tmp_path / "license.lic"
    make_license(path, max_domains=99)
    monkeypatch.setattr(licensing, "LICENSE_FILE", path)
    licensing.reset_cache()

    allowed, _ = licensing.check_domain_limit(db)
    assert allowed


# --- Установка файла --------------------------------------------------------------- #


def test_install_rejects_invalid_without_touching_current(monkeypatch, tmp_path):
    current = tmp_path / "license.lic"
    make_license(current)
    monkeypatch.setattr(licensing, "LICENSE_FILE", current)
    licensing.reset_cache()

    bad = tmp_path / "новая.lic"
    bad.write_bytes(b"garbage")
    result = licensing.install_license_file(bad)

    assert not result.valid
    assert licensing.status(use_cache=False).valid  # старая лицензия цела


def test_install_replaces_current_with_valid(monkeypatch, tmp_path):
    target = tmp_path / "license.lic"
    monkeypatch.setattr(licensing, "LICENSE_FILE", target)
    licensing.reset_cache()

    fresh = tmp_path / "новая.lic"
    make_license(fresh, max_domains=7)
    result = licensing.install_license_file(fresh)

    assert result.valid
    assert target.is_file()
    assert licensing.status(use_cache=False).max_domains == 7
