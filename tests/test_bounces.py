"""Разбор отчётов о невозможности доставки (RFC 3464).

Образцы — в том виде, в каком их шлют Postfix и Exchange: разный порядок
частей, разное оформление кодов, разная полнота полей.
"""

from email import policy
from email.parser import BytesParser

import pytest

from app.constants import Direction, MessageStatus
from app.models import Message
from app.services import bounces, receiving
from tests.conftest import make_mailbox

ORIGINAL_ID = "<out-42@test-gw.kz>"


@pytest.fixture
def mailbox(db):
    """Ящик, на который приходит отчёт: это обратный адрес наших писем."""
    make_mailbox(db, address="sales@test-gw.kz", is_active=True)
    db.commit()


def parse(raw: str):
    return bounces.parse(BytesParser(policy=policy.default).parsebytes(raw.encode()))


def postfix_bounce(status="5.1.1", diagnostic="smtp; 550 5.1.1 User unknown",
                   original_id=ORIGINAL_ID) -> str:
    return f"""From: MAILER-DAEMON@mx.outside.example
To: sales@test-gw.kz
Subject: Undelivered Mail Returned to Sender
Message-ID: <bounce-1@mx.outside.example>
Content-Type: multipart/report; report-type=delivery-status; boundary="B1"
MIME-Version: 1.0

--B1
Content-Type: text/plain; charset=utf-8

This is the mail system at host mx.outside.example.

--B1
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.outside.example

Final-Recipient: rfc822; client@outside.example
Action: failed
Status: {status}
Diagnostic-Code: {diagnostic}

--B1
Content-Type: message/rfc822

From: sales@test-gw.kz
To: client@outside.example
Subject: Коммерческое предложение
Message-ID: {original_id}

Текст письма.

--B1--
"""


def ordinary_letter() -> str:
    return """From: client@outside.example
To: sales@test-gw.kz
Subject: Undelivered Mail Returned to Sender
Message-ID: <normal@outside.example>
Content-Type: text/plain; charset=utf-8

Это обычное письмо, а вовсе не отчёт о недоставке.
"""


# --- Опознание ----------------------------------------------------------------- #


def test_delivery_report_is_recognised():
    assert parse(postfix_bounce()) is not None


def test_ordinary_letter_is_not_treated_as_a_report():
    """Тема у обычной переписки бывает какой угодно.

    Опознание по теме пометило бы живое письмо как отказ доставки.
    """
    assert parse(ordinary_letter()) is None


def test_report_without_status_part_is_ignored():
    raw = postfix_bounce().replace("Content-Type: message/delivery-status", "Content-Type: text/plain")
    assert parse(raw) is None


# --- Разбор полей --------------------------------------------------------------- #


def test_fields_are_extracted():
    bounce = parse(postfix_bounce())

    assert bounce.original_message_id == ORIGINAL_ID
    assert bounce.recipient == "client@outside.example"
    assert bounce.status == "5.1.1"
    assert "User unknown" in bounce.diagnostic
    assert bounce.action == "failed"


def test_permanent_and_temporary_are_told_apart():
    """`Action: failed` приходит и на временную помеху — решает код."""
    assert parse(postfix_bounce(status="5.1.1")).is_permanent is True
    assert parse(postfix_bounce(status="4.2.2")).is_permanent is False


def test_status_is_extracted_from_noisy_value():
    bounce = parse(postfix_bounce(status="5.1.1 (bad destination mailbox address)"))
    assert bounce.status == "5.1.1"


def test_original_id_falls_back_to_the_field():
    """Копию оригинала прикладывают не все серверы."""
    raw = postfix_bounce()
    raw = raw.split("--B1\nContent-Type: message/rfc822")[0] + "--B1--\n"
    raw = raw.replace("Action: failed", f"Original-Message-ID: {ORIGINAL_ID}\nAction: failed")

    assert parse(raw).original_message_id == ORIGINAL_ID


# --- Влияние на исходное письмо -------------------------------------------------- #


@pytest.fixture
def sent_message(db):
    message = Message(
        direction=Direction.OUTGOING, status=MessageStatus.SENT,
        sender="sales@test-gw.kz", recipient="client@outside.example",
        subject="Коммерческое предложение", message_id=ORIGINAL_ID,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def test_permanent_bounce_marks_the_original(db, sent_message):
    bounces.apply(db, parse(postfix_bounce()))
    db.commit()

    db.expire_all()
    original = db.get(Message, sent_message.id)
    assert original.status == MessageStatus.BOUNCED
    assert original.bounce_status == "5.1.1"
    assert original.bounced_at is not None
    assert "User unknown" in original.bounce_diagnostic


def test_temporary_bounce_does_not_bury_the_letter(db, sent_message):
    """Принимающая сторона ещё пытается доставить — письмо не потеряно."""
    bounces.apply(db, parse(postfix_bounce(status="4.2.2", diagnostic="smtp; 452 Mailbox full")))
    db.commit()

    db.expire_all()
    original = db.get(Message, sent_message.id)
    assert original.status == MessageStatus.SENT
    assert original.bounce_status == "4.2.2"  # но след остаётся


def test_report_for_unknown_message_is_harmless(db):
    assert bounces.apply(db, parse(postfix_bounce(original_id="<чужое@example.org>"))) is None


def test_incoming_message_is_never_marked_bounced(db):
    """Отчёт может относиться только к тому, что отправляли мы."""
    db.add(Message(direction=Direction.INCOMING, status=MessageStatus.RECEIVED,
                   sender="a@outside.example", recipient="sales@test-gw.kz",
                   message_id=ORIGINAL_ID))
    db.commit()

    assert bounces.apply(db, parse(postfix_bounce())) is None


# --- Приём отчёта целиком --------------------------------------------------------- #


def test_report_arriving_by_smtp_updates_the_original(db, sent_message, mailbox):
    """Сквозной путь: отчёт приходит как обычное письмо и отмечает оригинал."""
    result = receiving.accept(
        postfix_bounce().encode(), ["sales@test-gw.kz"], "MAILER-DAEMON@mx.outside.example"
    )
    assert result.outcome == receiving.Outcome.STORED

    db.expire_all()
    assert db.get(Message, sent_message.id).status == MessageStatus.BOUNCED


def test_report_itself_is_kept_as_incoming_mail(db, sent_message, mailbox):
    """Оператор должен видеть отчёт целиком, а не только отметку на письме."""
    receiving.accept(
        postfix_bounce().encode(), ["sales@test-gw.kz"], "MAILER-DAEMON@mx.outside.example"
    )

    from sqlalchemy import select

    stored = db.scalars(
        select(Message).where(Message.direction == Direction.INCOMING)
    ).all()
    assert len(stored) == 1
    assert stored[0].sender == "MAILER-DAEMON@mx.outside.example"


def test_broken_report_does_not_reject_the_letter(db, mailbox, monkeypatch):
    """Отчёт уже принят по SMTP — терять его из-за сбоя разбора нельзя."""
    monkeypatch.setattr(bounces, "parse", _boom)

    result = receiving.accept(
        postfix_bounce().encode(), ["sales@test-gw.kz"], "MAILER-DAEMON@mx.outside.example"
    )
    assert result.outcome == receiving.Outcome.STORED


def _boom(*_args, **_kwargs):
    raise RuntimeError("разбор сломался")
