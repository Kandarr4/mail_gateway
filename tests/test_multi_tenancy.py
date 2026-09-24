"""Изоляция клиентов (F-54): ключ одного клиента не видит данных другого.

Главный сценарий продажи нескольким клиентам: у каждого свои домены, ящики,
письма и вложения, и ни один валидный ключ не должен дотянуться до чужого —
ни списком, ни прямым обращением по номеру, ни перебором идентификаторов.
Чужая сущность всегда выглядит как несуществующая (404), а не запрещённая
(403): сам факт существования — уже утечка.
"""

import pytest
from sqlalchemy import select

from app.constants import Direction, MessageStatus
from app.models import Attachment, Client, Message
from app.services import api_keys, clients, domains
from tests.conftest import AUTH, make_mailbox

TENANT_B_DOMAIN = "tenant-b.example"


@pytest.fixture
def tenant_b(db):
    """Второй клиент со своим доменом и ключом. Возвращает (заголовки, клиент)."""
    client_row = clients.create(db, "Tenant B")
    domains.create(db, client_row.id, TENANT_B_DOMAIN)
    _, raw = api_keys.create(db, client_row.id, "test-key-b")
    db.commit()
    return {"Authorization": f"Bearer {raw}"}, client_row


def _send_as_default(client, **overrides) -> dict:
    payload = {
        "sender": "sales@test-gw.kz",
        "recipient": "someone@outside.example",
        "subject": "Письмо клиента A",
        "body_text": "содержимое",
        **overrides,
    }
    response = client.post("/api/v1/messages", headers=AUTH, json=payload)
    assert response.status_code == 202, response.text
    return response.json()


# --- Ящики ----------------------------------------------------------------------- #


def test_client_b_cannot_list_client_a_mailboxes(client, db, tenant_b):
    auth_b, _ = tenant_b
    make_mailbox(db, address="sales@test-gw.kz", is_active=True)
    db.commit()

    page = client.get("/api/v1/mailboxes", headers=auth_b).json()
    assert page["total"] == 0
    assert page["items"] == []


def test_client_b_cannot_get_client_a_mailbox_by_address(client, db, tenant_b):
    auth_b, _ = tenant_b
    make_mailbox(db, address="sales@test-gw.kz", is_active=True)
    db.commit()

    response = client.get("/api/v1/mailboxes/sales@test-gw.kz", headers=auth_b)
    assert response.status_code == 404  # не 403: существование не подтверждаем


def test_mailbox_creation_rejects_foreign_domain(client, tenant_b):
    """Ящик на чужом домене создать нельзя — даже валидным ключом."""
    auth_b, _ = tenant_b
    response = client.put(
        "/api/v1/mailboxes/intruder@test-gw.kz", headers=auth_b, json={"is_active": True}
    )
    assert response.status_code == 422


def test_mailbox_creation_rejects_unregistered_domain(client):
    response = client.put(
        "/api/v1/mailboxes/box@nowhere.example", headers=AUTH, json={"is_active": True}
    )
    assert response.status_code == 422


def test_mailbox_creation_works_on_own_domain(client, tenant_b):
    auth_b, _ = tenant_b
    response = client.put(
        f"/api/v1/mailboxes/info@{TENANT_B_DOMAIN}", headers=auth_b, json={"is_active": True}
    )
    assert response.status_code == 201


# --- Письма ---------------------------------------------------------------------- #


def test_client_b_cannot_send_as_client_a_domain(client, tenant_b):
    auth_b, _ = tenant_b
    response = client.post(
        "/api/v1/messages",
        headers=auth_b,
        json={
            "sender": "sales@test-gw.kz",  # домен клиента A
            "recipient": "someone@outside.example",
            "body_text": "попытка подлога",
        },
    )
    assert response.status_code == 422


def test_client_b_cannot_read_client_a_message_by_id(client, tenant_b):
    auth_b, _ = tenant_b
    message_id = _send_as_default(client)["id"]

    assert client.get(f"/api/v1/messages/{message_id}", headers=auth_b).status_code == 404
    # Собственный ключ письмо видит.
    assert client.get(f"/api/v1/messages/{message_id}", headers=AUTH).status_code == 200


def test_client_b_cannot_patch_client_a_message(client, tenant_b):
    auth_b, _ = tenant_b
    message_id = _send_as_default(client)["id"]

    response = client.patch(
        f"/api/v1/messages/{message_id}", headers=auth_b, json={"is_read": True}
    )
    assert response.status_code == 404


def test_client_b_cannot_list_client_a_messages(client, tenant_b):
    auth_b, _ = tenant_b
    _send_as_default(client)

    page = client.get("/api/v1/messages", headers=auth_b).json()
    assert page["total"] == 0


def test_sender_owns_outgoing_message_even_without_mailbox(client):
    """Исходящее письмо видно отправившему клиенту, даже если записи `Mailbox`
    для отправителя нет: договор API (202 → GET по Location) не должен
    зависеть от реестра ящиков."""
    message_id = _send_as_default(client)["id"]
    response = client.get(f"/api/v1/messages/{message_id}", headers=AUTH)
    assert response.status_code == 200


def test_unowned_message_is_invisible_to_every_key(client, db, tenant_b):
    """Письмо без владельца (принято до регистрации ящика) не видно никому."""
    auth_b, _ = tenant_b
    orphan = Message(
        client_id=None,
        direction=Direction.INCOMING,
        status=MessageStatus.RECEIVED,
        sender="a@outside.example",
        recipient="lost@nowhere.example",
        message_id="<orphan@outside.example>",
    )
    db.add(orphan)
    db.commit()

    for headers in (AUTH, auth_b):
        assert client.get(f"/api/v1/messages/{orphan.id}", headers=headers).status_code == 404


# --- Вложения -------------------------------------------------------------------- #


def test_client_b_cannot_download_client_a_attachment_by_guessed_id(client, db, tenant_b, tmp_path):
    """Главный сценарий атаки: перебор целочисленных id вложений."""
    auth_b, _ = tenant_b
    secret = tmp_path / "секрет.pdf"
    secret.write_bytes(b"confidential")

    message_id = _send_as_default(client)["id"]
    message = db.get(Message, message_id)
    message.attachments.append(
        Attachment(filename="секрет.pdf", filepath=str(secret),
                   content_type="application/pdf", size=12)
    )
    db.commit()
    attachment_id = db.scalars(select(Attachment.id)).first()

    response = client.get(f"/api/v1/attachments/{attachment_id}", headers=auth_b)
    assert response.status_code == 404
    assert b"confidential" not in response.content


def test_client_b_cannot_attach_client_a_upload(client, tenant_b):
    """Чужой upload_id нельзя прикрепить к своему письму."""
    auth_b, _ = tenant_b
    upload = client.post(
        "/api/v1/uploads", headers=AUTH, files={"file": ("doc.txt", b"data", "text/plain")}
    ).json()

    response = client.post(
        "/api/v1/messages",
        headers=auth_b,
        json={
            "sender": f"info@{TENANT_B_DOMAIN}",
            "recipient": "someone@outside.example",
            "body_text": "тело",
            "attachment_ids": [upload["id"]],
        },
    )
    assert response.status_code == 422


# --- Ключи ----------------------------------------------------------------------- #


def test_revoked_api_key_is_rejected(client, db, tenant_b):
    auth_b, client_row = tenant_b
    assert client.get("/api/v1/mailboxes", headers=auth_b).status_code == 200

    for key in api_keys.list_for_client(db, client_row.id):
        api_keys.revoke(db, key)
    db.commit()

    assert client.get("/api/v1/mailboxes", headers=auth_b).status_code == 401


def test_deactivated_client_keys_stop_working(client, db, tenant_b):
    """Отключение клиента отзывает доступ всех его ключей разом."""
    auth_b, client_row = tenant_b
    clients.set_active(db, client_row.id, False)
    db.commit()

    assert client.get("/api/v1/mailboxes", headers=auth_b).status_code == 401


def test_last_used_at_updates_on_authenticated_request(client, db, tenant_b):
    auth_b, client_row = tenant_b
    key = api_keys.list_for_client(db, client_row.id)[0]
    assert key.last_used_at is None

    client.get("/api/v1/mailboxes", headers=auth_b)
    db.expire_all()
    assert api_keys.list_for_client(db, client_row.id)[0].last_used_at is not None


def test_env_bootstrap_created_default_client(db):
    """`MG_API_TOKENS` превращается в клиента «Default» с рабочими ключами."""
    default = db.scalars(select(Client).where(Client.name == "Default")).one()
    assert domains.by_name(db, "test-gw.kz").client_id == default.id
    assert api_keys.by_hash(db, "token-a").client_id == default.id
    assert api_keys.by_hash(db, "token-b").client_id == default.id


# --- Домены ---------------------------------------------------------------------- #


def test_domain_cannot_be_registered_to_two_clients(db, tenant_b):
    from app.api.errors import Conflict

    other = clients.create(db, "Tenant C")
    with pytest.raises(Conflict):
        domains.create(db, other.id, TENANT_B_DOMAIN)
