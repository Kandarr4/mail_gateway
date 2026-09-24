"""Воркер отправки исходящих писем (F-16…F-21)."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import Direction, MessageStatus
from ..db import utcnow
from ..models import Message
from ..services.delivery import PermanentSendError, deliver
from .base import Job, QueueWorker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendJob(Job):
    recipient: str


class OutboxWorker(QueueWorker[SendJob]):
    name = "outbox"
    model = Message
    permanent_errors = (PermanentSendError,)

    @property
    def max_attempts(self) -> int:
        return settings.send_max_attempts

    @property
    def retry_base_seconds(self) -> int:
        return settings.send_retry_base_seconds

    @property
    def retry_max_seconds(self) -> int:
        return settings.send_retry_max_seconds

    @property
    def concurrency(self) -> int:
        return settings.send_concurrency

    def pause_reason(self) -> str:
        """Без действующей лицензии письма ждут в очереди, а не проваливаются.

        Пауза, а не отказ: письмо остаётся `pending` и уйдёт само, как только
        лицензию установят или продлят. Помечать его `failed` было бы потерей
        чужой почты из-за нашей коммерции.
        """
        from ..services import licensing

        return "" if licensing.mail_allowed() else licensing.suspension_notice()

    def narrow(self, stmt):
        return stmt.where(Message.direction == Direction.OUTGOING)

    def extract(self, row: Message) -> SendJob:
        return SendJob(id=row.id, recipient=row.recipient)

    async def handle(self, job: SendJob) -> None:
        # smtplib блокирующая — уводим в поток, чтобы не вставал цикл событий.
        await asyncio.to_thread(self._deliver, job.id)

    def after_finish(self, db: Session, row: Message, status: str, error: str | None) -> None:
        """Итог отправки виден через API: `status`, `sent_at`, `last_error`."""
        if status == MessageStatus.SENT:
            row.sent_at = utcnow()

    @staticmethod
    def _deliver(message_id: int) -> None:
        from ..db import SessionLocal
        from ..services import domains
        from ..utils import domain_of

        with SessionLocal() as db:
            message = db.get(Message, message_id)
            if message is None:
                raise PermanentSendError("Письмо исчезло из БД во время отправки")
            # Домен и его ключ DKIM разрешаются на момент отправки, а не
            # постановки в очередь: ключ могли заменить, пока письмо ждало.
            domain = domains.by_name(db, domain_of(message.sender))
            if domain is None:
                logger.warning(
                    "Домен отправителя %s не зарегистрирован — письмо id=%s уйдёт без DKIM",
                    message.sender, message.id,
                )
            deliver(message, domain)
