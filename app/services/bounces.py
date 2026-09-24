"""Разбор отчётов о невозможности доставки (RFC 3464).

Без этого статус `sent` означает всего лишь «следующий сервер принял письмо».
Отказ приходит позже и отдельным письмом, и если его не разбирать, шлюз
продолжает считать несуществующий адрес рабочим: письма уходят «успешно» и
пропадают, а отправитель об этом не знает.
"""

import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import MessageStatus
from ..db import utcnow
from ..models import Message

logger = logging.getLogger(__name__)

REPORT_TYPE = "multipart/report"
STATUS_TYPE = "message/delivery-status"
ORIGINAL_TYPE = "message/rfc822"

#: `5.1.1` и подобное. Первая цифра — класс: 5 постоянный, 4 временный.
_STATUS = re.compile(r"\b([245])\.\d{1,3}\.\d{1,3}\b")
_MAX_DIAGNOSTIC = 2000


@dataclass(frozen=True)
class Bounce:
    """Разобранный отчёт."""

    original_message_id: str | None
    recipient: str | None
    status: str | None
    diagnostic: str | None
    action: str

    @property
    def is_permanent(self) -> bool:
        """Адресата не существует — повторять бессмысленно.

        Опираемся на код состояния, а не на слово `failed` в `Action`: временный
        отказ («почтовый ящик переполнен») тоже приходит как `failed`, и
        считать по нему адрес мёртвым нельзя.
        """
        return bool(self.status and self.status.startswith("5"))


def parse(msg: EmailMessage) -> Bounce | None:
    """Разбирает письмо как отчёт о доставке либо возвращает None.

    Опознаём строго по типу `multipart/report; report-type=delivery-status`.
    Угадывать по теме («Undelivered Mail Returned to Sender») нельзя: под это
    описание попадает и обычная переписка, и автоответчик, а ценой ошибки
    будет чужое живое письмо, помеченное как отказ доставки.
    """
    if msg.get_content_type() != REPORT_TYPE:
        return None
    if (msg.get_param("report-type") or "").lower() != "delivery-status":
        return None

    status_part = _part_of_type(msg, STATUS_TYPE)
    if status_part is None:
        logger.warning("Отчёт о доставке без части %s — пропущен", STATUS_TYPE)
        return None

    fields = _status_fields(status_part)
    return Bounce(
        original_message_id=_original_message_id(msg, fields),
        recipient=_address(fields.get("final-recipient") or fields.get("original-recipient")),
        status=_code(fields.get("status")),
        diagnostic=_diagnostic(fields),
        action=(fields.get("action") or "").strip().lower(),
    )


def apply(db: Session, bounce: Bounce) -> Message | None:
    """Отмечает исходное письмо как не доставленное. None — оригинал не найден.

    Не найти оригинал — нормально: отчёт мог прийти на письмо, отправленное
    другой системой, или база уже почищена по сроку хранения. Это не ошибка и
    не повод отказывать в приёме отчёта.
    """
    if not bounce.original_message_id:
        return None

    original = db.scalars(
        select(Message).where(Message.message_id == bounce.original_message_id)
    ).first()
    if original is None:
        logger.info("Отчёт о доставке для неизвестного письма %s", bounce.original_message_id)
        return None
    if not original.is_outgoing:
        # Отчёт может ссылаться только на то, что отправляли мы.
        return None

    original.bounced_at = utcnow()
    original.bounce_status = bounce.status
    original.bounce_diagnostic = bounce.diagnostic

    if bounce.is_permanent:
        original.status = MessageStatus.BOUNCED
        logger.warning(
            "Письмо id=%s не доставлено на %s: %s %s",
            original.id, original.recipient, bounce.status, bounce.diagnostic or "",
        )
    else:
        # Временную задержку статусом не отражаем: принимающая сторона ещё
        # пытается доставить, и письмо не потеряно.
        logger.info(
            "Задержка доставки письма id=%s на %s: %s",
            original.id, original.recipient, bounce.status,
        )
    return original


def _part_of_type(msg: EmailMessage, content_type: str):
    for part in msg.walk():
        if part.get_content_type() == content_type:
            return part
    return None


def _status_fields(part) -> dict[str, str]:
    """Поля отчёта одним словарём.

    Часть `message/delivery-status` состоит из групп полей: общей и по одной
    на получателя. Нас интересует первый отказавший получатель, поэтому
    группы сливаются, а более поздние значения перекрывают ранние.
    """
    fields: dict[str, str] = {}
    payload = part.get_payload()

    groups = payload if isinstance(payload, list) else []
    if not groups and isinstance(payload, str):
        # Некоторые серверы отдают часть плоским текстом, а не группами.
        from email.parser import Parser

        groups = [Parser().parsestr(payload)]

    for group in groups:
        for name, value in group.items():
            fields[name.lower()] = value
    return fields


def _original_message_id(msg: EmailMessage, fields: dict[str, str]) -> str | None:
    """Message-ID письма, которое не дошло.

    Надёжнее всего он лежит в приложенной копии оригинала; поле
    `Original-Message-ID` заполняют не все.
    """
    original = _part_of_type(msg, ORIGINAL_TYPE)
    if original is not None:
        payload = original.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if inner is not None and inner.get("Message-ID"):
            return inner["Message-ID"].strip()

    value = fields.get("original-message-id")
    return value.strip() if value else None


def _address(raw: str | None) -> str | None:
    """`rfc822; user@example.org` → `user@example.org`."""
    if not raw:
        return None
    return raw.partition(";")[2].strip().strip("<>").lower() or None


def _code(raw: str | None) -> str | None:
    if not raw:
        return None
    found = _STATUS.search(raw)
    return found.group(0) if found else None


def _diagnostic(fields: dict[str, str]) -> str | None:
    raw = fields.get("diagnostic-code") or fields.get("status")
    if not raw:
        return None
    return " ".join(raw.split())[:_MAX_DIAGNOSTIC]
