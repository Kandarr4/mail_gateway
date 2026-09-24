"""Реестр ящиков, загрузки и вложения (F-14, F-15, F-31, F-35)."""

from datetime import timedelta

from sqlalchemy import select

from app.db import utcnow
from app.models import Upload
from app.services import uploads
from tests.conftest import AUTH

# --- Ящики ------------------------------------------------------------------ #


def test_created_mailbox_reports_201_and_location(client):
    response = client.put("/api/v1/mailboxes/Sales@Test-GW.kz", headers=AUTH, json={})
    assert response.status_code == 201
    assert response.headers["Location"] == "/api/v1/mailboxes/sales@test-gw.kz"
    assert response.json()["address"] == "sales@test-gw.kz"


def test_put_is_idempotent(client):
    """Повторный PUT — то же состояние и 200 вместо 201."""
    body = {"external_id": "1", "display_name": "Продажи"}
    first = client.put("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json=body)
    second = client.put("/api/v1/mailboxes/A@test-gw.kz", headers=AUTH, json=body)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    page = client.get("/api/v1/mailboxes", headers=AUTH).json()
    assert page["total"] == 1


def test_put_replaces_state(client):
    """PUT задаёт состояние целиком: непереданное поле возвращается к умолчанию."""
    client.put("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json={"external_id": "1"})
    replaced = client.put("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json={"display_name": "Отдел"})

    assert replaced.json()["external_id"] is None
    assert replaced.json()["display_name"] == "Отдел"


def test_get_mailbox_by_address(client):
    client.put("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json={"external_id": "7"})
    assert client.get("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH).json()["external_id"] == "7"
    assert client.get("/api/v1/mailboxes/нет@test-gw.kz", headers=AUTH).status_code == 404


def test_delete_mailbox(client):
    client.put("/api/v1/mailboxes/d@test-gw.kz", headers=AUTH, json={})
    assert client.delete("/api/v1/mailboxes/d@test-gw.kz", headers=AUTH).status_code == 204
    assert client.delete("/api/v1/mailboxes/d@test-gw.kz", headers=AUTH).status_code == 404


def test_mailboxes_are_paginated(client):
    for index in range(3):
        client.put(f"/api/v1/mailboxes/box{index}@test-gw.kz", headers=AUTH, json={})

    page = client.get("/api/v1/mailboxes", headers=AUTH, params={"limit": 2}).json()
    assert len(page["items"]) == 2
    assert page["total"] == 3


# --- Загрузки и вложения ---------------------------------------------------- #


def _upload(client, name="прайс.txt", data=b"price-list"):
    return client.post("/api/v1/uploads", headers=AUTH, files={"file": (name, data, "text/plain")})


def test_upload_reports_201_and_location(client):
    response = _upload(client)
    assert response.status_code == 201
    assert response.headers["Location"] == f"/api/v1/uploads/{response.json()['id']}"


def test_upload_then_attach_then_download(client):
    upload_id = _upload(client).json()["id"]

    message_id = client.post(
        "/api/v1/messages", headers=AUTH,
        json={"sender": "s@test-gw.kz", "recipient": "c@outside.example",
              "body_text": "Каталог во вложении", "attachment_ids": [upload_id]},
    ).json()["id"]

    attachments = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()["attachments"]
    assert len(attachments) == 1

    downloaded = client.get(f"/api/v1/attachments/{attachments[0]['id']}", headers=AUTH)
    assert downloaded.status_code == 200
    assert downloaded.content == b"price-list"


def test_upload_cannot_be_reused(client):
    upload_id = _upload(client).json()["id"]
    body = {"sender": "s@test-gw.kz", "recipient": "c@outside.example",
            "body_text": "x", "attachment_ids": [upload_id]}

    assert client.post("/api/v1/messages", headers=AUTH, json=body).status_code == 202
    assert client.post("/api/v1/messages", headers=AUTH, json=body).status_code == 422


def test_unknown_upload_id_rejected(client):
    body = {"sender": "s@test-gw.kz", "recipient": "c@outside.example",
            "body_text": "x", "attachment_ids": [999999]}
    assert client.post("/api/v1/messages", headers=AUTH, json=body).status_code == 422


def test_missing_attachment_is_404(client):
    assert client.get("/api/v1/attachments/999999", headers=AUTH).status_code == 404


def test_attachment_with_deleted_file_is_404(client):
    """Файл мог исчезнуть — наружу это «не найдено», а не 500."""
    from pathlib import Path

    upload_id = _upload(client).json()["id"]
    message_id = client.post(
        "/api/v1/messages", headers=AUTH,
        json={"sender": "s@test-gw.kz", "recipient": "c@outside.example",
              "body_text": "x", "attachment_ids": [upload_id]},
    ).json()["id"]
    attachment = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()["attachments"][0]

    from app.db import SessionLocal
    from app.models import Attachment

    with SessionLocal() as session:
        Path(session.get(Attachment, attachment["id"]).filepath).unlink()

    assert client.get(f"/api/v1/attachments/{attachment['id']}", headers=AUTH).status_code == 404


# --- Чистка брошенных загрузок ---------------------------------------------- #


def test_expired_unused_uploads_are_purged(client, db):
    from pathlib import Path

    upload_id = _upload(client, "брошенный.txt").json()["id"]
    stored = db.get(Upload, upload_id)
    path = Path(stored.filepath)
    stored.created_at = utcnow() - timedelta(days=30)
    db.commit()

    assert uploads.purge_expired() == 1
    assert not path.exists()
    assert db.scalars(select(Upload)).all() == []


def test_used_uploads_are_kept(client, db):
    upload_id = _upload(client).json()["id"]
    client.post(
        "/api/v1/messages", headers=AUTH,
        json={"sender": "s@test-gw.kz", "recipient": "c@outside.example",
              "body_text": "x", "attachment_ids": [upload_id]},
    )
    db.get(Upload, upload_id).created_at = utcnow() - timedelta(days=30)
    db.commit()

    assert uploads.purge_expired() == 0
