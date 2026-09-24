"""Роуты писем. Тонкие: разбор входа → сервис → ответ."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ...models import Client
from ...schemas import MessageFilter, MessageOut, MessagePatch, Page, SendRequest, SendResponse
from ...services import licensing, messages
from ..deps import get_db, protected, require_client
from ..errors import ServiceUnavailable
from ..loaders import load_message

router = APIRouter(prefix="/messages", tags=["Письма"], dependencies=protected)


@router.post("", response_model=SendResponse, status_code=202)
def send_message(
    payload: SendRequest,
    response: Response,
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
) -> SendResponse:
    """Ставит письмо в очередь отправки; фактическая доставка — асинхронно.

    `202` вместо `201`: ресурс создан, но обработка не завершена — итог
    появится в `GET /messages/{id}`, на который указывает `Location`.
    """
    # Без действующей лицензии честнее отказать сразу: письмо, молча осевшее
    # в очереди навсегда, интегрирующая система считает отправленным.
    if not licensing.mail_allowed():
        raise ServiceUnavailable(licensing.suspension_notice())

    message = messages.queue_outgoing(db, client.id, payload)
    response.headers["Location"] = f"/api/v1/messages/{message.id}"
    return SendResponse.model_validate(message, from_attributes=True)


@router.get("", response_model=Page[MessageOut])
def list_messages(
    filters: MessageFilter = Depends(),
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
):
    return messages.get_page(db, filters, client.id)


@router.get("/{message_id}", response_model=MessageOut)
def get_message(
    message_id: int, client: Client = Depends(require_client), db: Session = Depends(get_db)
):
    return load_message(db, message_id, client.id)


@router.patch("/{message_id}", response_model=MessageOut)
def patch_message(
    message_id: int,
    patch: MessagePatch,
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
):
    """Частичное изменение письма, например отметка о прочтении."""
    return messages.apply_patch(load_message(db, message_id, client.id), patch)
