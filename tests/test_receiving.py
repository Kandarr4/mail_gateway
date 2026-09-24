"""Приём входящей почты (F-04…F-09) — без поднятия SMTP-сервера."""

from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Message
from app.services import receiving, storage
from app.services.receiving import Outcome
from tests.conftest import make_mailbox

MAILBOX = "sales@test-gw.kz"


@pytest.fixture
def mailbox(db):
    box = make_mailbox(db, address=MAILBOX, external_id="42", is_active=True)
    db.commit()
    return box


def build(subject="Заявка", message_id="<incoming-1@outside.example>", **kwargs) -> bytes:
    msg = EmailMessage()
    msg["From"] = kwargs.get("sender", "client@outside.example")
    msg["To"] = kwargs.get("to", MAILBOX)
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    for header in ("In-Reply-To", "References"):
        if value := kwargs.get(header.lower().replace("-", "_")):
            msg[header] = value
    msg.set_content(kwargs.get("body", "Здравствуйте!"))
    if html := kwargs.get("html"):
        msg.add_alternative(html, subtype="html")
    for name, data in kwargs.get("attachments", []):
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes()


def accept(raw: bytes, recipient: str = MAILBOX):
    return receiving.accept(raw, [recipient], "client@outside.example")


def test_stores_message(mailbox, db):
    result = accept(build())
    assert result.outcome is Outcome.STORED

    message = db.get(Message, result.message_id)
    assert message.direction == "incoming"
    assert message.subject == "Заявка"
    assert "Здравствуйте" in message.body_text
    assert message.external_id == "42"


def test_duplicate_message_id_is_not_stored_twice(mailbox, db):
    raw = build()
    assert accept(raw).outcome is Outcome.STORED
    assert accept(raw).outcome is Outcome.DUPLICATE
    assert db.scalar(select(Message).where(Message.direction == "incoming").limit(1)) is not None
    assert len(db.scalars(select(Message)).all()) == 1


def test_unknown_mailbox_rejected(db):
    assert accept(build(to="nobody@test-gw.kz"), "nobody@test-gw.kz").outcome is Outcome.UNKNOWN_MAILBOX


def test_missing_message_id_is_generated(mailbox, db):
    result = accept(build(message_id=None))
    message = db.get(Message, result.message_id)
    assert message.message_id.endswith("@test-gw.kz>")


def test_both_bodies_preserved(mailbox, db):
    result = accept(build(html="<p>Привет, <b>мир</b></p>"))
    message = db.get(Message, result.message_id)
    assert message.body_text
    assert message.body_html and "<b>" in message.body_html


def test_html_only_letter_gets_text_fallback(mailbox, db):
    msg = EmailMessage()
    msg["From"] = "client@outside.example"
    msg["To"] = MAILBOX
    msg["Subject"] = "Только HTML"
    msg["Message-ID"] = "<html-only@outside.example>"
    msg.set_content("<p>Строка<br>Вторая</p>", subtype="html")

    result = accept(msg.as_bytes())
    message = db.get(Message, result.message_id)
    assert "Строка" in message.body_text


def test_thread_key_taken_from_references(mailbox, db):
    raw = build(references="<root@outside.example> <mid@outside.example>", in_reply_to="<mid@outside.example>")
    message = db.get(Message, accept(raw).message_id)
    assert message.thread_key == "<root@outside.example>"


def test_attachments_stored_and_named_uniquely(mailbox, db):
    """Два вложения с одинаковым именем не должны затирать друг друга."""
    raw = build(attachments=[("отчёт.bin", b"first"), ("отчёт.bin", b"second")])
    message = db.get(Message, accept(raw).message_id)

    assert len(message.attachments) == 2
    paths = {a.filepath for a in message.attachments}
    assert len(paths) == 2
    assert {storage.read(Path(p)) for p in paths} == {b"first", b"second"}


def test_oversized_attachment_skipped_but_letter_accepted(mailbox, db):
    """Письмо принимаем, теряем только вложение (F-09)."""
    raw = build(attachments=[("big.bin", b"x" * (2 * 1024 * 1024))])
    result = accept(raw)
    assert result.outcome is Outcome.STORED
    assert db.get(Message, result.message_id).attachments == []


def test_summary_reports_attachments(mailbox, db):
    """has_attachments считается до commit — легко было получить false."""
    message = db.get(Message, accept(build(attachments=[("a.bin", b"x")])).message_id)
    assert receiving.summary(message)["has_attachments"] is True
    assert receiving.summary(message)["received_at"].endswith("+00:00")
