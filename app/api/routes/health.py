"""Наблюдаемость: /health для оператора, /ready для балансировщика."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import __version__
from ...config import settings
from ...constants import MessageStatus
from ...db import check_connection
from ...models import Message
from ...schemas import HealthOut, ReadyOut
from ..deps import get_db, protected

router = APIRouter(tags=["Служебное"])

#: Заполняется при старте: признак того, что SMTP-слушатель поднялся.
_state = {"smtp": False}


def set_smtp_ready(value: bool) -> None:
    _state["smtp"] = value


@router.get("/health", response_model=HealthOut, dependencies=protected)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """Состояние сервиса и очередей (F-36)."""
    return HealthOut(
        status="ok",
        version=__version__,
        domain=settings.domain,
        outbound_mode=str(settings.outbound_mode),
        queued=_count(db, Message, Message.status == MessageStatus.QUEUED),
        sending=_count(db, Message, Message.status == MessageStatus.SENDING),
        failed=_count(db, Message, Message.status == MessageStatus.FAILED),
        last_message_id=db.scalar(select(func.max(Message.id))) or 0,
    )


@router.get("/ready", response_model=ReadyOut)
def ready() -> ReadyOut:
    """Готовность принимать нагрузку. Без токена — вызывается инфраструктурой."""
    database = check_connection()
    return ReadyOut(ready=database and _state["smtp"], database=database, smtp=_state["smtp"])


def _count(db: Session, model, condition) -> int:
    return db.scalar(select(func.count()).select_from(model).where(condition)) or 0
