"""Роуты реестра локальных ящиков. Ресурс адресуется почтовым адресом."""

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy.orm import Session

from ...models import Client
from ...schemas import MailboxIn, MailboxOut, Page, PageParams
from ...services import mailboxes
from ..deps import get_db, protected, require_client
from ..loaders import load_mailbox

router = APIRouter(prefix="/mailboxes", tags=["Ящики"], dependencies=protected)

_ADDRESS = Path(..., description="Почтовый адрес ящика", examples=["sales@somnium.kz"])


@router.get("", response_model=Page[MailboxOut])
def list_mailboxes(
    params: PageParams = Depends(), client: Client = Depends(require_client), db: Session = Depends(get_db)
):
    return mailboxes.get_page(db, params, client.id)


@router.get("/{address}", response_model=MailboxOut)
def get_mailbox(
    address: str = _ADDRESS, client: Client = Depends(require_client), db: Session = Depends(get_db)
):
    return load_mailbox(db, address, client.id)


@router.put("/{address}", response_model=MailboxOut)
def put_mailbox(
    payload: MailboxIn,
    response: Response,
    address: str = _ADDRESS,
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
):
    """Приводит ящик к заданному виду. Идемпотентно: повтор даёт тот же результат."""
    mailbox, created = mailboxes.upsert(db, address, payload, client.id)
    if created:
        response.status_code = 201
        response.headers["Location"] = f"/api/v1/mailboxes/{mailbox.address}"
    return mailbox


@router.delete("/{address}", status_code=204)
def delete_mailbox(
    address: str = _ADDRESS, client: Client = Depends(require_client), db: Session = Depends(get_db)
) -> None:
    mailboxes.delete(db, load_mailbox(db, address, client.id))
