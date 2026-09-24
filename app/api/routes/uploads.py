"""Загрузка файлов, которые затем прикладываются к письму по идентификатору."""

from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy.orm import Session

from ...models import Client
from ...schemas import UploadOut
from ...services import uploads
from ..deps import get_db, protected, require_client

router = APIRouter(prefix="/uploads", tags=["Вложения"], dependencies=protected)


@router.post("", response_model=UploadOut, status_code=201)
async def upload_file(
    response: Response,
    file: UploadFile = File(...),
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
):
    upload = await uploads.create(db, client.id, file)
    response.headers["Location"] = f"/api/v1/uploads/{upload.id}"
    return upload
