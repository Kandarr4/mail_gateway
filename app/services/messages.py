"""Бизнес-логика работы с письмами.

Слой ничего не знает про HTTP: принимает данные, бросает доменные исключения,
возвращает модели. Commit делает вызывающая единица работы.
"""

import logging

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..api.errors import ValidationError
from ..config import settings
from ..constants import Direction, MessageStatus
from ..db import utcnow
from ..models import Attachment, Domain, Mailbox, Message
from ..schemas import MessageFilter, MessagePatch, PageResult, SendRequest
from ..utils import domain_of, new_message_id
from . import domains, mailboxes, uploads

logger = logging.getLogger(__name__)


def queue_outgoing(db: Session, client_id: int, payload: SendRequest) -> Message:
    """Ставит письмо в очередь отправки (F-11…F-15). Доставка — воркером."""
    _validate_sender(db, client_id, payload.sender)
    if not payload.body_text and not payload.body_html:
        raise ValidationError("Письмо не может быть пустым: нужен body_text или body_html")

    mailbox = mailboxes.by_address(db, payload.sender)
    message = Message(
        client_id=client_id,
        direction=Direction.OUTGOING,
        status=MessageStatus.QUEUED,
        sender=payload.sender,
        recipient=payload.recipient,
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
        message_id=new_message_id(),
        in_reply_to=payload.in_reply_to,
        thread_key=payload.in_reply_to,
        mailbox_id=mailbox.id if mailbox else None,
        external_id=payload.external_id or (mailbox.external_id if mailbox else None),
        next_attempt_at=utcnow(),
    )
    db.add(message)
    db.flush()

    if message.thread_key is None:  # письмо само является корнем переписки (F-07)
        message.thread_key = message.message_id

    for upload_id in payload.attachment_ids:
        upload = uploads.take_unused(db, client_id, upload_id)
        message.attachments.append(
            Attachment(
                filename=upload.filename,
                filepath=upload.filepath,
                content_type=upload.content_type,
                size=upload.size,
            )
        )
    db.flush()

    logger.info("Письмо id=%s поставлено в очередь для %s", message.id, message.recipient)
    return message


def get_page(db: Session, filters: MessageFilter, client_id: int | None = None) -> PageResult:
    items = list(
        db.scalars(
            _apply_filters(select(Message), filters, client_id)
            .order_by(_ordering(filters))
            .offset(filters.offset)
            .limit(filters.limit)
        )
    )
    total = db.scalar(_apply_filters(select(func.count()).select_from(Message), filters, client_id)) or 0
    return PageResult(items=items, total=total, limit=filters.limit, offset=filters.offset)


def _ordering(filters: MessageFilter):
    """Порядок выдачи зависит от того, листают ленту или догоняют очередь.

    При опросе по `since_id` порядок обязан быть по возрастанию. Иначе на
    `since_id=42` с сотней новых писем и `limit=50` клиент получил бы *самые
    новые* пятьдесят, сдвинул курсор на максимальный id — и полсотни писем
    между ними не увидел бы уже никогда.

    Без `since_id` запрос означает «покажи последние» — там естественнее
    убывание. Именно `is not None`, а не проверка на истинность: `since_id=0` —
    законный курсор «с самого начала», с которого опрос и начинается.
    """
    return Message.id.desc() if filters.since_id is None else Message.id.asc()


def apply_patch(message: Message, patch: MessagePatch) -> Message:
    """Частичное изменение письма. Меняется только то, что явно передали."""
    if patch.is_read is not None:
        message.is_read = patch.is_read
    return message


def count_by_status(db: Session, status: MessageStatus) -> int:
    return db.scalar(select(func.count()).select_from(Message).where(Message.status == status)) or 0


def requeue_stuck_sending(db: Session) -> int:
    """Возвращает в очередь письма, застрявшие в статусе `sending`.

    Такие остаются после аварийной остановки процесса между «взял в работу» и
    «отправил»: без этого письмо не подхватит ни один воркер и оно зависнет
    навсегда. Счётчик попыток уже увеличен, поэтому бесконечного цикла нет.
    """
    stuck = list(db.scalars(select(Message).where(Message.status == MessageStatus.SENDING)))
    for message in stuck:
        if message.attempts_exhausted(settings.send_max_attempts):
            message.mark_failed("Процесс остановлен во время отправки, попытки исчерпаны")
        else:
            message.status = MessageStatus.QUEUED
            message.next_attempt_at = utcnow()
    if stuck:
        logger.warning("Возвращено в очередь после перезапуска: %s писем", len(stuck))
    return len(stuck)


def _validate_sender(db: Session, client_id: int, sender: str) -> Domain:
    """Домен отправителя обязан быть зарегистрирован за этим клиентом (F-12,
    F-54). Это же правило закрывает шлюз от использования как открытого
    релея и не даёт одному клиенту отправлять письма от чужого домена.
    """
    domain = domains.by_name(db, domain_of(sender))
    if domain is None or domain.client_id != client_id or not domain.is_active:
        raise ValidationError(
            "Отправитель должен быть в одном из доменов вашего клиента",
            errors={"sender": "домен не зарегистрирован за вашим клиентом"},
        )
    return domain


def _apply_filters(stmt: Select, filters: MessageFilter, client_id: int | None = None) -> Select:
    if client_id is not None:
        stmt = stmt.where(Message.client_id == client_id)
    if filters.mailbox:
        stmt = stmt.join(Mailbox, Message.mailbox_id == Mailbox.id).where(
            Mailbox.address == mailboxes.normalize(filters.mailbox)
        )
    if filters.since_id is not None:
        stmt = stmt.where(Message.id > filters.since_id)
    if filters.direction:
        stmt = stmt.where(Message.direction == filters.direction)
    if filters.status:
        stmt = stmt.where(Message.status == filters.status)
    if filters.recipient:
        stmt = stmt.where(Message.recipient == filters.recipient.lower())
    if filters.unread_only:
        stmt = stmt.where(Message.is_read.is_(False))
    if filters.dmarc_result:
        stmt = stmt.where(Message.dmarc_result == filters.dmarc_result)
    if filters.search:
        # `%` и `_` из пользовательского ввода — метасимволы LIKE: без экранирования
        # запрос «50%» нашёл бы вообще всё. Escape-символ задаём явно, иначе SQLite
        # его не применит.
        needle = filters.search.strip().replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{needle}%"
        stmt = stmt.where(
            Message.subject.ilike(pattern, escape="\\")
            | Message.sender.ilike(pattern, escape="\\")
            | Message.recipient.ilike(pattern, escape="\\")
        )
    return stmt
