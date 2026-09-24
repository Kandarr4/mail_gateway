"""Шифрование содержимого: тела писем в БД и файлы вложений на диске.

Весь остальной прогон работает с включённым ключом (conftest), поэтому здесь —
только то, что оттуда не видно: что в БД и на диске действительно шифротекст,
совместимость со старыми открытыми данными и режим без ключа.
"""

import pytest
from sqlalchemy import text

from app import crypto
from app.constants import Direction, MessageStatus
from app.db import SessionLocal
from app.models import Message
from app.services import storage


def make_message(db, default_client_id, **fields) -> Message:
    message = Message(
        direction=Direction.INCOMING,
        status=MessageStatus.RECEIVED,
        sender="from@outside.example",
        recipient="to@test-gw.kz",
        client_id=default_client_id,
        **fields,
    )
    db.add(message)
    db.commit()
    return message


# --- Тексты в БД ------------------------------------------------------------------- #


def test_bodies_are_ciphertext_in_db_but_plaintext_in_orm(db, default_client_id):
    message = make_message(
        db, default_client_id,
        body_text="секретный текст", body_html="<b>секрет</b>", bounce_diagnostic="разгадка",
    )

    raw = db.execute(
        text("SELECT body_text, body_html, bounce_diagnostic FROM message WHERE id = :id"),
        {"id": message.id},
    ).one()
    for column in raw:
        assert column.startswith(crypto.TEXT_PREFIX)
        assert "секрет" not in column and "разгадка" not in column

    with SessionLocal() as fresh:
        reread = fresh.get(Message, message.id)
        assert reread.body_text == "секретный текст"
        assert reread.body_html == "<b>секрет</b>"
        assert reread.bounce_diagnostic == "разгадка"


def test_metadata_stays_searchable_plaintext(db, default_client_id):
    """Адреса и тема не шифруются — по ним работают поиск и фильтры."""
    message = make_message(db, default_client_id, subject="квартальный отчёт")
    raw = db.execute(
        text("SELECT sender, recipient, subject FROM message WHERE id = :id"),
        {"id": message.id},
    ).one()
    assert raw.sender == "from@outside.example"
    assert raw.subject == "квартальный отчёт"


def test_legacy_plaintext_row_still_readable(db, default_client_id):
    """Строка, записанная до включения шифрования, читается как есть."""
    message = make_message(db, default_client_id)
    db.execute(
        text("UPDATE message SET body_text = 'старое открытое тело' WHERE id = :id"),
        {"id": message.id},
    )
    db.commit()
    with SessionLocal() as fresh:
        assert fresh.get(Message, message.id).body_text == "старое открытое тело"


def test_none_stays_none(db, default_client_id):
    message = make_message(db, default_client_id, body_text=None)
    with SessionLocal() as fresh:
        assert fresh.get(Message, message.id).body_text is None


# --- Файлы вложений ---------------------------------------------------------------- #


def test_stored_file_is_ciphertext_on_disk():
    path = storage.store(b"attachment payload", "file.bin", "box@test-gw.kz")
    on_disk = path.read_bytes()
    assert on_disk.startswith(crypto.FILE_MAGIC)
    assert b"attachment payload" not in on_disk
    assert storage.read(path) == b"attachment payload"
    assert b"".join(storage.stream(path)) == b"attachment payload"


def test_multi_chunk_roundtrip(tmp_path):
    """Файл больше куска шифруется по частям и собирается без потерь."""
    payload = bytes(range(256)) * 1024  # 256 КиБ > UPLOAD_CHUNK_BYTES
    path = tmp_path / "big.bin"
    crypto.write_file(path, payload)
    assert crypto.read_file(path) == payload


def test_legacy_plaintext_file_still_readable(tmp_path):
    path = tmp_path / "legacy.bin"
    path.write_bytes(b"plain old bytes")
    assert not crypto.is_encrypted_file(path)
    assert crypto.read_file(path) == b"plain old bytes"


def test_empty_file_roundtrip(tmp_path):
    path = tmp_path / "empty.bin"
    crypto.write_file(path, b"")
    assert crypto.read_file(path) == b""


# --- Режим без ключа --------------------------------------------------------------- #


def test_no_key_passthrough(monkeypatch):
    monkeypatch.setattr(crypto, "fernet", lambda: None)
    assert crypto.encrypt_text("как есть") == "как есть"
    assert crypto.decrypt_text("как есть") == "как есть"


def test_no_key_plain_file(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "fernet", lambda: None)
    path = tmp_path / "plain.bin"
    crypto.write_file(path, b"no key data")
    assert path.read_bytes() == b"no key data"


def test_encrypted_data_without_key_fails_loudly(monkeypatch, tmp_path):
    """Без ключа зашифрованное не выдаётся ни молча, ни мусором — только ошибка."""
    token = crypto.encrypt_text("секрет")
    path = tmp_path / "enc.bin"
    crypto.write_file(path, b"secret bytes")

    monkeypatch.setattr(crypto, "fernet", lambda: None)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_text(token)
    with pytest.raises(crypto.DecryptionError):
        crypto.read_file(path)


def test_uploaded_attachment_encrypted_end_to_end(client, db):
    """Через API: загрузка → на диске шифротекст → скачивание отдаёт оригинал."""
    from pathlib import Path

    from app.models import Attachment
    from tests.conftest import AUTH

    upload = client.post(
        "/api/v1/uploads",
        headers=AUTH,
        files={"file": ("данные.txt", b"very secret attachment", "text/plain")},
    )
    assert upload.status_code == 201, upload.text

    sent = client.post(
        "/api/v1/messages",
        headers=AUTH,
        json={
            "sender": "enc@test-gw.kz",
            "recipient": "out@outside.example",
            "subject": "тест",
            "body_text": "тело",
            "attachment_ids": [upload.json()["id"]],
        },
    )
    assert sent.status_code == 202, sent.text
    message = client.get(f"/api/v1/messages/{sent.json()['id']}", headers=AUTH).json()
    attachment_id = message["attachments"][0]["id"]

    on_disk = Path(db.get(Attachment, attachment_id).filepath).read_bytes()
    assert on_disk.startswith(crypto.FILE_MAGIC)
    assert b"very secret attachment" not in on_disk

    downloaded = client.get(f"/api/v1/attachments/{attachment_id}", headers=AUTH)
    assert downloaded.status_code == 200
    assert downloaded.content == b"very secret attachment"
