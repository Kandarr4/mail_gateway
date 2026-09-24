"""Отдача вложений. Путь к файлу роут не строит — этим занимается загрузчик."""

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...models import Client
from ..deps import get_db, protected, require_client
from ..loaders import attachment_response, load_attachment_file

router = APIRouter(prefix="/attachments", tags=["Вложения"], dependencies=protected)


@router.get("/{attachment_id}")
def download_attachment(
    attachment_id: int, client: Client = Depends(require_client), db: Session = Depends(get_db)
) -> Response:
    attachment, path = load_attachment_file(db, attachment_id, client.id)
    return attachment_response(
        path, attachment.filename, attachment.content_type or "application/octet-stream"
    )
