"""Файлы, загруженные до формирования письма."""

import logging
from datetime import timedelta

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.errors import PayloadTooLarge, ValidationError
from ..config import settings
from ..constants import UPLOAD_CHUNK_BYTES
from ..db import session_scope, utcnow
from ..models import Upload
from ..utils import sanitize_filename
from . import storage

logger = logging.getLogger(__name__)


async def create(db: Session, client_id: int, source: UploadFile) -> Upload:
    """Сохраняет загружаемый файл потоком.

    Читать тело целиком в память нельзя: несколько параллельных загрузок по
    `MG_MAX_ATTACHMENT_MB` каждая исчерпали бы её. Лимит проверяется по ходу
    записи, недописанный файл удаляется.
    """
    safe_name = sanitize_filename(source.filename or "unnamed")
    path = storage.store_outgoing_stream(safe_name)
    limit = settings.max_attachment_bytes
    size = 0

    try:
        with storage.open_sink(path) as target:
            while chunk := await source.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise PayloadTooLarge(f"Файл больше {settings.max_attachment_mb} МБ")
                target.write(chunk)
    except Exception:
        storage.remove(str(path))
        raise

    upload = Upload(
        client_id=client_id,
        filename=safe_name,
        filepath=str(path),
        content_type=source.content_type,
        size=size,
    )
    db.add(upload)
    db.flush()
    return upload


def take_unused(db: Session, client_id: int, upload_id: int) -> Upload:
    """Забирает свободный файл под письмо и помечает использованным (F-14, F-15).

    Чужой `upload_id` — тот же ответ, что и несуществующий: подбором номера
    нельзя ни подтвердить его существование, ни прикрепить чужой файл (F-54).
    """
    upload = db.get(Upload, upload_id)
    if upload is None or upload.client_id != client_id:
        raise ValidationError(f"Файл {upload_id} не найден", attachment_id=upload_id)
    if upload.is_used:
        raise ValidationError(
            f"Файл {upload_id} уже прикреплён к письму", attachment_id=upload_id
        )
    upload.is_used = True
    return upload


def purge_expired() -> int:
    """Удаляет непривязанные загрузки старше `MG_UPLOAD_TTL_HOURS` вместе с файлами.

    Без этого каталог `outgoing/` растёт от каждой брошенной загрузки.
    """
    cutoff = utcnow() - timedelta(hours=settings.upload_ttl_hours)
    removed = 0
    with session_scope() as db:
        stale = db.scalars(
            select(Upload).where(Upload.is_used.is_(False), Upload.created_at < cutoff)
        ).all()
        for upload in stale:
            storage.remove(upload.filepath)
            db.delete(upload)
            removed += 1
    if removed:
        logger.info("Очищено неиспользованных загрузок: %s", removed)
        storage.purge_empty_dirs()
    return removed
