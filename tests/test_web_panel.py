"""Веб-панель: доступ, сессии, CSRF, хранение паролей."""

import pytest

from app.api.errors import ValidationError
from app.constants import Direction, MessageStatus
from app.models import AdminUser, Attachment, Message
from app.services import admin_users
from app.services.passwords import hash_password, needs_rehash, verify_password
from app.web import session as web_session
from app.web.routes import _login_limiter
from tests.conftest import make_mailbox

USERNAME = "operator"
PASSWORD = "Pa55word-Test"


@pytest.fixture(autouse=True)
def _clean_sessions(client):
    """Клиент общий на весь прогон — cookie, сессии и счётчик попыток чистим.

    Без сброса лимитера тесты упираются в защиту от перебора паролей:
    входов здесь заметно больше десяти в минуту с одного адреса.
    """
    web_session.clear_all()
    _login_limiter.reset()
    client.cookies.clear()
    yield
    web_session.clear_all()
    _login_limiter.reset()
    client.cookies.clear()


@pytest.fixture
def admin(db):
    db.add(AdminUser(username=USERNAME, password_hash=hash_password(PASSWORD)))
    db.commit()


@pytest.fixture
def logged_in(client, admin):
    response = client.post("/admin/login", data={"username": USERNAME, "password": PASSWORD},
                           follow_redirects=False)
    assert response.status_code == 303
    return client


def csrf_of(client) -> str:
    """Достаёт токен CSRF из формы на странице ящиков."""
    html = client.get("/admin/").text
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


# --- Хранение паролей -------------------------------------------------------- #


def test_password_is_never_stored_in_plaintext(db, admin):
    stored = admin_users.by_username(db, USERNAME)
    assert PASSWORD not in stored.password_hash
    assert stored.password_hash.startswith("scrypt$")


def test_same_password_gives_different_hashes():
    """Соль обязана быть случайной, иначе одинаковые пароли видны по хешу."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_accepts_correct_and_rejects_wrong():
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored) is True
    assert verify_password(PASSWORD + "x", stored) is False
    assert verify_password("", stored) is False


def test_verify_survives_garbage_hash():
    for junk in (None, "", "не-хеш", "scrypt$сломано"):
        assert verify_password(PASSWORD, junk) is False


def test_needs_rehash_detects_foreign_format():
    assert needs_rehash("bcrypt$2b$12$whatever") is True
    assert needs_rehash(hash_password(PASSWORD)) is False


@pytest.mark.parametrize(
    ("weak", "reason"),
    [
        ("Кор1", "короче десяти символов"),
        ("1234567890", "одни цифры"),
        ("abcdefghij", "одни буквы"),
    ],
)
def test_weak_passwords_rejected(db, weak, reason):
    with pytest.raises(ValidationError):
        admin_users.create(db, "newuser", weak)


# --- Вход ---------------------------------------------------------------------- #


def test_login_page_available(client):
    assert client.get("/admin/login").status_code == 200


def test_wrong_password_does_not_authenticate(client, admin):
    response = client.post("/admin/login", data={"username": USERNAME, "password": "неверный"},
                           follow_redirects=False)
    assert response.status_code == 200
    assert "Неверное имя пользователя или пароль" in response.text
    assert web_session.COOKIE_NAME not in response.cookies


def test_unknown_user_gives_same_message(client, admin):
    """Разные сообщения позволили бы перебором выяснить существующие имена."""
    response = client.post("/admin/login", data={"username": "нет-такого", "password": PASSWORD})
    assert "Неверное имя пользователя или пароль" in response.text


def test_disabled_account_cannot_log_in(client, db, admin):
    admin_users.by_username(db, USERNAME).is_active = False
    db.commit()

    response = client.post("/admin/login", data={"username": USERNAME, "password": PASSWORD},
                           follow_redirects=False)
    assert response.status_code == 200


def test_login_sets_hardened_cookie(client, admin):
    response = client.post("/admin/login", data={"username": USERNAME, "password": PASSWORD},
                           follow_redirects=False)
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header          # недоступна из JavaScript
    assert "SameSite=strict" in header   # не уйдёт с чужого сайта
    assert "Path=/admin" in header


def test_last_login_recorded(client, db, admin):
    client.post("/admin/login", data={"username": USERNAME, "password": PASSWORD})
    db.expire_all()
    assert admin_users.by_username(db, USERNAME).last_login_at is not None


# --- Доступ ------------------------------------------------------------------- #


@pytest.mark.parametrize("url", ["/admin/", "/admin/password"])
def test_pages_require_login(client, url):
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_forms_require_login(client):
    response = client.post("/admin/mailboxes", data={"address": "a@test-gw.kz"},
                           follow_redirects=False)
    assert response.status_code == 303


def test_logout_invalidates_session_on_server(logged_in):
    """Сессия должна умирать на сервере, а не только в браузере."""
    assert logged_in.get("/admin/", follow_redirects=False).status_code == 200
    logged_in.post("/admin/logout", follow_redirects=False)
    assert logged_in.get("/admin/", follow_redirects=False).status_code == 303


def test_forged_cookie_rejected(client, admin):
    """Идентификатор сессии проверяется по серверному хранилищу, а не по подписи."""
    try:
        client.cookies.set(web_session.COOKIE_NAME, "forged-session-id", path="/admin")
        assert client.get("/admin/", follow_redirects=False).status_code == 303
    finally:
        client.cookies.clear()


# --- CSRF ---------------------------------------------------------------------- #


def test_create_without_csrf_rejected(logged_in, db):
    response = logged_in.post("/admin/mailboxes",
                              data={"address": "csrf@test-gw.kz", "is_active": "true"})
    assert response.status_code == 400
    assert admin_users.by_username(db, USERNAME) is not None
    from app.services import mailboxes

    assert mailboxes.by_address(db, "csrf@test-gw.kz") is None


def test_create_with_wrong_csrf_rejected(logged_in, db):
    response = logged_in.post(
        "/admin/mailboxes",
        data={"address": "csrf2@test-gw.kz", "csrf_token": "чужой-токен", "is_active": "true"},
    )
    assert response.status_code == 400


def test_non_ascii_csrf_rejected_not_crashed(logged_in):
    """Кириллица в токене формы должна давать отказ, а не ошибку сервера."""
    response = logged_in.post(
        "/admin/mailboxes",
        data={"address": "crash@test-gw.kz", "csrf_token": "токен-кириллицей"},
    )
    assert response.status_code == 400


def test_delete_without_csrf_rejected(logged_in, db):
    make_mailbox(db, address="keep@test-gw.kz", is_active=True)
    db.commit()

    response = logged_in.post("/admin/mailboxes/keep@test-gw.kz/delete")
    assert response.status_code == 400

    from app.services import mailboxes

    db.expire_all()
    assert mailboxes.by_address(db, "keep@test-gw.kz") is not None


# --- Управление ящиками --------------------------------------------------------- #


def test_create_mailbox_through_panel(logged_in, db):
    token = csrf_of(logged_in)
    response = logged_in.post(
        "/admin/mailboxes",
        data={"address": "Sales@Test-GW.kz", "display_name": "Продажи",
              "external_id": "42", "is_active": "true", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    from app.services import mailboxes

    mailbox = mailboxes.by_address(db, "sales@test-gw.kz")
    assert mailbox is not None
    assert mailbox.address == "sales@test-gw.kz"  # приведён к нижнему регистру
    assert mailbox.external_id == "42"


def test_unchecked_active_creates_disabled_mailbox(logged_in, db):
    token = csrf_of(logged_in)
    logged_in.post("/admin/mailboxes",
                   data={"address": "off@test-gw.kz", "csrf_token": token})

    from app.services import mailboxes

    assert mailboxes.by_address(db, "off@test-gw.kz").is_active is False


def test_delete_mailbox_through_panel(logged_in, db):
    make_mailbox(db, address="gone@test-gw.kz", is_active=True)
    db.commit()

    token = csrf_of(logged_in)
    logged_in.post("/admin/mailboxes/gone@test-gw.kz/delete", data={"csrf_token": token})

    from app.services import mailboxes

    db.expire_all()
    assert mailboxes.by_address(db, "gone@test-gw.kz") is None


def test_mailboxes_listed(logged_in, db):
    make_mailbox(db, address="shown@test-gw.kz", display_name="Видно", is_active=True)
    db.commit()

    html = logged_in.get("/admin/").text
    assert "shown@test-gw.kz" in html
    assert "Видно" in html


# --- Смена пароля --------------------------------------------------------------- #


def test_change_password(logged_in, db):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/password", data={
        "current_password": PASSWORD, "new_password": "Nov4ya-Parol",
        "repeat_password": "Nov4ya-Parol", "csrf_token": token,
    })
    assert "Пароль изменён" in response.text

    db.expire_all()
    assert admin_users.authenticate(db, USERNAME, "Nov4ya-Parol") is not None


def test_change_password_requires_current(logged_in):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/password", data={
        "current_password": "неверный", "new_password": "Nov4ya-Parol",
        "repeat_password": "Nov4ya-Parol", "csrf_token": token,
    })
    assert "Текущий пароль неверен" in response.text


def test_change_password_rejects_mismatch(logged_in):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/password", data={
        "current_password": PASSWORD, "new_password": "Nov4ya-Parol",
        "repeat_password": "Drugaya-Parol1", "csrf_token": token,
    })
    assert "Пароли не совпадают" in response.text


# --- Просмотр почты --------------------------------------------------------------- #


@pytest.fixture
def incoming(db):
    """Входящее письмо с враждебным HTML-телом и вложением."""
    make_mailbox(db, address="box@test-gw.kz", is_active=True)
    message = Message(
        direction=Direction.INCOMING,
        status=MessageStatus.RECEIVED,
        sender="chuzhoy@example.org",
        recipient="box@test-gw.kz",
        subject="Счёт на оплату",
        body_text="Текстовая часть",
        body_html="<p>Здравствуйте</p><script>alert(document.cookie)</script>",
        message_id="<in-1@example.org>",
        spf_result="pass",
        dkim_result="pass",
        dmarc_result="pass",
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def test_message_list_shows_mail(logged_in, incoming):
    html = logged_in.get("/admin/messages").text
    assert "Счёт на оплату" in html
    assert "chuzhoy@example.org" in html


def test_message_detail_shows_headers_and_text(logged_in, incoming):
    html = logged_in.get(f"/admin/messages/{incoming.id}").text
    assert "Текстовая часть" in html
    assert "box@test-gw.kz" in html


def test_hostile_html_never_inlined_into_panel_page(logged_in, incoming):
    """Тело письма не должно попадать в разметку панели ни в каком виде.

    Иначе `<script>` из письма выполнится с правами администратора: сессия
    панели живёт в cookie того же происхождения.
    """
    html = logged_in.get(f"/admin/messages/{incoming.id}").text
    assert "alert(document.cookie)" not in html
    assert "<script>alert" not in html
    assert f'sandbox src="/admin/messages/{incoming.id}/body"' in html


def test_body_frame_is_sandboxed_and_locked_down(logged_in, incoming):
    response = logged_in.get(f"/admin/messages/{incoming.id}/body")
    policy = response.headers["Content-Security-Policy"]
    assert "sandbox" in policy
    assert "default-src 'none'" in policy
    # Внешние загрузки запрещены: картинка-маячок не подтвердит прочтение письма.
    assert "img-src data:" in policy
    # Собственный фрейм показать можно, чужой сайт нас встроить не может.
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_admin_pages_allow_own_frame_only(logged_in):
    policy = logged_in.get("/admin/messages").headers["Content-Security-Policy"]
    assert "frame-src 'self'" in policy


def test_message_pages_require_login(client, incoming):
    for url in ("/admin/messages", f"/admin/messages/{incoming.id}",
                f"/admin/messages/{incoming.id}/body", "/admin/compose"):
        assert client.get(url, follow_redirects=False).status_code == 303


def test_search_filters_by_subject(logged_in, incoming, db):
    db.add(Message(direction=Direction.INCOMING, status=MessageStatus.RECEIVED,
                   sender="a@example.org", recipient="box@test-gw.kz", subject="Другое"))
    db.commit()

    html = logged_in.get("/admin/messages", params={"search": "Счёт"}).text
    assert "Счёт на оплату" in html
    assert "Другое" not in html


def test_search_treats_wildcards_literally(logged_in, incoming):
    """`%` из поля поиска — это символ, а не «найти всё»."""
    html = logged_in.get("/admin/messages", params={"search": "%"}).text
    assert "Счёт на оплату" not in html


def test_unknown_filter_value_is_ignored_not_fatal(logged_in, incoming):
    """Строку запроса правит кто угодно — мусор не должен ронять страницу."""
    response = logged_in.get("/admin/messages", params={"status": "выдумка"})
    assert response.status_code == 200
    assert "Счёт на оплату" in response.text


def test_attachment_served_as_download_only(logged_in, incoming, db):
    from app.services import storage

    payload = b"<html><script>alert(1)</script></html>"
    path = storage.store(payload, "otchet.html", "test")
    attachment = Attachment(message_id_fk=incoming.id, filename="otchet.html",
                            filepath=str(path), content_type="text/html", size=len(payload))
    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    response = logged_in.get(f"/admin/attachments/{attachment.id}")
    assert response.status_code == 200
    # HTML-вложение, отданное как text/html, исполнилось бы в контексте панели.
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]


def test_attachment_requires_login(client, incoming):
    assert client.get("/admin/attachments/1", follow_redirects=False).status_code == 303


# --- Отправка почты --------------------------------------------------------------- #


@pytest.fixture
def sender_box(db):
    make_mailbox(db, address="sales@test-gw.kz", display_name="Продажи", is_active=True)
    db.commit()


def test_compose_sends_message(logged_in, db, sender_box):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/compose", data={
        "sender": "sales@test-gw.kz", "recipient": "client@example.org",
        "subject": "Коммерческое предложение", "body_text": "Текст письма",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303

    from sqlalchemy import select

    message = db.scalars(select(Message).where(Message.direction == Direction.OUTGOING)).one()
    assert message.recipient == "client@example.org"
    assert message.status == MessageStatus.QUEUED
    assert response.headers["location"] == f"/admin/messages/{message.id}"


def test_compose_with_attachment(logged_in, db, sender_box):
    token = csrf_of(logged_in)
    logged_in.post(
        "/admin/compose",
        data={"sender": "sales@test-gw.kz", "recipient": "client@example.org",
              "subject": "Договор", "body_text": "Во вложении", "csrf_token": token},
        files={"files": ("dogovor.pdf", b"%PDF-1.4 test", "application/pdf")},
    )

    from sqlalchemy import select

    message = db.scalars(select(Message).where(Message.direction == Direction.OUTGOING)).one()
    assert [a.filename for a in message.attachments] == ["dogovor.pdf"]


def test_compose_rejects_foreign_sender_domain(logged_in, db, sender_box):
    """Панель не должна обходить защиту от открытого релея."""
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/compose", data={
        "sender": "kto-to@chuzhoy.kz", "recipient": "client@example.org",
        "body_text": "Текст", "csrf_token": token,
    })
    assert response.status_code == 200
    assert "test-gw.kz" in response.text

    from sqlalchemy import select

    assert db.scalars(select(Message)).all() == []


def test_compose_reports_bad_recipient_without_crashing(logged_in, db, sender_box):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/compose", data={
        "sender": "sales@test-gw.kz", "recipient": "не-адрес",
        "body_text": "Текст", "csrf_token": token,
    })
    assert response.status_code == 200
    assert "Неверный адрес получателя" in response.text


def test_compose_requires_csrf(logged_in, db, sender_box):
    response = logged_in.post("/admin/compose", data={
        "sender": "sales@test-gw.kz", "recipient": "client@example.org",
        "body_text": "Текст",
    })
    assert response.status_code == 400

    from sqlalchemy import select

    assert db.scalars(select(Message)).all() == []


def test_reply_prefills_form(logged_in, incoming):
    html = logged_in.get(f"/admin/compose?reply_to={incoming.id}").text
    assert "Re: Счёт на оплату" in html
    assert "chuzhoy@example.org" in html


# --- Резервные копии --------------------------------------------------------------- #


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "backup_path", tmp_path / "backups", raising=False)
    return tmp_path / "backups"


def test_backup_page_shows_settings(logged_in, backup_dir):
    html = logged_in.get("/admin/backup").text
    assert "Размер резервной копии" in html


def test_size_limit_is_saved(logged_in, db, backup_dir):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/backup/settings", data={
        "backup.enabled": "true", "backup.interval_hours": "6",
        "backup.max_total_mb": "2048", "csrf_token": token,
    })
    assert "Настройки сохранены" in response.text

    from app.services import app_settings

    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 2048


def test_invalid_limit_is_reported_not_saved(logged_in, db, backup_dir):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/backup/settings", data={
        "backup.interval_hours": "6", "backup.max_total_mb": "0", "csrf_token": token,
    })
    assert response.status_code == 200

    from app.services import app_settings

    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 1024


def test_snapshot_can_be_made_from_the_panel(logged_in, backup_dir):
    token = csrf_of(logged_in)
    response = logged_in.post("/admin/backup/run", data={"csrf_token": token})

    assert "Снимок создан" in response.text

    from app.services import backup

    assert len(backup.existing()) == 1


def test_backup_download_refuses_paths_outside_the_directory(logged_in, backup_dir):
    """Имя из адреса нельзя подставлять в путь: так утекает любой файл с диска."""
    for attempt in ("../../.env", "..%2F..%2F.env", "mail_gateway-нет.db"):
        response = logged_in.get(f"/admin/backup/{attempt}", follow_redirects=False)
        assert response.status_code in (404, 400), attempt


def test_backup_pages_require_login(client, backup_dir):
    assert client.get("/admin/backup", follow_redirects=False).status_code == 303
    assert client.post("/admin/backup/run", follow_redirects=False).status_code == 303


def test_backup_settings_require_csrf(logged_in, db, backup_dir):
    response = logged_in.post("/admin/backup/settings", data={"backup.max_total_mb": "4096"})
    assert response.status_code == 400

    from app.services import app_settings

    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 1024


# --- Заголовки безопасности ------------------------------------------------------ #


def test_security_headers_present(client):
    headers = client.get("/admin/login").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_panel_hidden_from_openapi(client):
    """Панель — не часть программного контракта."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/admin") for path in paths)
