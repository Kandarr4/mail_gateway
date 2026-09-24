"""Разбор входящего письма и его сохранение — без привязки к SMTP-транспорту.

Отделено от `app/smtp_server.py` намеренно: логика приёма проверяется тестами
без поднятия сервера, а транспорт остаётся тонким.
"""

import logging
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import Direction, InboundAuthPolicy, MessageStatus
from ..db import session_scope
from ..models import Attachment, Message
from ..utils import html_to_text, new_message_id, sanitize_filename
from . import bounces, inbound_auth, mailboxes, storage
from .inbound_auth import UNCHECKED, AuthVerdict

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    """Что делать транспорту с принятым письмом."""

    STORED = "stored"
    DUPLICATE = "duplicate"
    UNKNOWN_MAILBOX = "unknown_mailbox"
    NOT_AUTHENTIC = "not_authentic"


@dataclass(frozen=True)
class Received:
    outcome: Outcome
    message_id: int | None = None


def accept(
    raw: bytes,
    envelope_recipients: list[str],
    envelope_sender: str,
    *,
    peer_ip: str | None = None,
    helo: str | None = None,
) -> Received:
    """Принимает письмо целиком: проверяет отправителя, разбирает, сохраняет."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)

    recipient = (envelope_recipients[0] if envelope_recipients else parseaddr(msg["To"])[1] or "").lower()
    sender = parseaddr(msg["From"])[1] or envelope_sender

    verdict = inbound_auth.verify(raw, peer_ip=peer_ip, mail_from=envelope_sender, helo=helo)
    if verdict.should_reject and settings.inbound_auth is InboundAuthPolicy.ENFORCE:
        logger.warning(
            "Отклонено письмо от %s для %s: %s (домен требует p=reject)",
            sender, recipient, inbound_auth.describe(verdict),
        )
        return Received(Outcome.NOT_AUTHENTIC)
    if verdict.suspicious:
        logger.warning(
            "Письмо от %s не прошло проверку: %s", sender, inbound_auth.describe(verdict)
        )

    with session_scope() as db:
        mailbox = mailboxes.by_address(db, recipient, active_only=True) if recipient else None
        if mailbox is None and settings.require_known_mailbox:
            return Received(Outcome.UNKNOWN_MAILBOX)

        message_id = (msg.get("Message-ID") or "").strip() or new_message_id()
        if _already_stored(db, message_id):
            logger.info("Дубликат Message-ID %s — письмо пропущено", message_id)
            return Received(Outcome.DUPLICATE)

        message = _build_message(msg, message_id, sender, recipient, mailbox, verdict)
        db.add(message)
        db.flush()

        _store_attachments(db, msg, message, recipient)
        _apply_bounce(db, msg)

        logger.info("Принято письмо id=%s от %s для %s", message.id, sender, recipient)
        return Received(Outcome.STORED, message.id)


def summary(message: Message) -> dict:
    """Краткое описание письма — для журнала и тестов."""
    return {
        "id": message.id,
        "recipient": message.recipient,
        "sender": message.sender,
        "subject": message.subject,
        "message_id": message.message_id,
        "thread_key": message.thread_key,
        "external_id": message.external_id,
        "received_at": message.created_at.isoformat(),
        "has_attachments": bool(message.attachments),
        "dmarc": message.dmarc_result,
    }


def _apply_bounce(db: Session, msg: EmailMessage) -> None:
    """Если письмо — отчёт о недоставке, отмечает исходное письмо.

    Сам отчёт всё равно сохраняется как обычное входящее: он адресован
    реальному ящику, и оператор должен видеть его целиком. Сбой разбора не
    должен отменять приём — отчёт уже принят, терять его нельзя.
    """
    try:
        bounce = bounces.parse(msg)
        if bounce is not None:
            bounces.apply(db, bounce)
    except Exception:  # приём важнее разбора
        logger.exception("Не удалось разобрать отчёт о недоставке")


def _already_stored(db: Session, message_id: str) -> bool:
    """Повторная доставка того же письма не должна создавать дубликат (F-06)."""
    return db.scalars(select(Message.id).where(Message.message_id == message_id)).first() is not None


def _build_message(
    msg: EmailMessage,
    message_id: str,
    sender: str,
    recipient: str,
    mailbox,
    verdict: AuthVerdict = UNCHECKED,
) -> Message:
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references = (msg.get("References") or "").split()
    # Ключ переписки — корневой Message-ID цепочки; без него письмо само корень (F-07).
    thread_key = references[0] if references else (in_reply_to or message_id)

    text, html = _extract_bodies(msg)
    return Message(
        # Владелец — клиент ящика-получателя; письмо без ящика (приём при
        # выключенном require_known_mailbox) не принадлежит никому и видно
        # только веб-панели.
        client_id=mailbox.client_id if mailbox else None,
        direction=Direction.INCOMING,
        status=MessageStatus.RECEIVED,
        sender=sender,
        recipient=recipient,
        subject=msg["Subject"],
        body_text=text,
        body_html=html or None,
        message_id=message_id,
        in_reply_to=in_reply_to,
        thread_key=thread_key,
        mailbox_id=mailbox.id if mailbox else None,
        external_id=mailbox.external_id if mailbox else None,
        spf_result=verdict.spf,
        dkim_result=verdict.dkim,
        dmarc_result=verdict.dmarc,
        auth_results=verdict.header or None,
    )


def _extract_bodies(msg: EmailMessage) -> tuple[str, str]:
    """Возвращает (текст, html); обе части сохраняются, ничего не теряется (F-04)."""
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))

    text = _content(text_part)
    html = _content(html_part)
    if not text and html:
        text = html_to_text(html)
    return text, html


def _content(part) -> str:
    if part is None:
        return ""
    try:
        return str(part.get_content()).strip()
    except (LookupError, ValueError) as exc:
        # Битая или неизвестная кодировка не должна ронять приём письма.
        logger.warning("Не удалось декодировать часть письма: %s", exc)
        return ""


def _store_attachments(db: Session, msg: EmailMessage, message: Message, recipient: str) -> None:
    for part in msg.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        if len(payload) > settings.max_attachment_bytes:
            # Письмо всё равно принимаем, теряем только вложение (F-09).
            logger.warning(
                "Вложение %s превышает лимит %s МБ — пропущено", filename, settings.max_attachment_mb
            )
            continue

        path = storage.store_incoming(payload, filename, recipient, message.id)
        # Через коллекцию, а не по внешнему ключу: иначе `message.attachments`
        # остаётся пустой до commit и событие уходит с has_attachments=false.
        message.attachments.append(
            Attachment(
                filename=sanitize_filename(filename),
                filepath=str(path),
                content_type=part.get_content_type(),
                size=len(payload),
            )
        )
    db.flush()
