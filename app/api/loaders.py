"""Загрузка ресурса по идентификатору — в одном месте.

Любой роут, которому нужна сущность, обязан пройти через `load_*`: так
«где-то проверили, где-то забыли» становится невозможным, а `404` не
копируется по обработчикам.

Каждый загрузчик принимает `client_id`: `None` — вызов из веб-панели, у неё
один общий администратор без разделения по клиентам, фильтра нет; целое
число — вызов из API, и тогда чужая сущность обязана выглядеть как
несуществующая (F-54), а не как «доступ запрещён» — иначе сам факт наличия
чужого ящика/письма/вложения уже был бы утечкой.
"""

from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from .. import crypto
from ..models import Attachment, Mailbox, Message
from ..services import mailboxes, storage
from .errors import NotFound


def load_message(db: Session, message_id: int, client_id: int | None = None) -> Message:
    message = db.get(Message, message_id)
    if message is None:
        raise NotFound("Письмо не найдено")
    # `Message.client_id` = NULL (письмо принято до регистрации получателя) не
    # относится ни к одному клиенту — значит, ни одному API-ключу оно не видно.
    if client_id is not None and message.client_id != client_id:
        raise NotFound("Письмо не найдено")
    return message


def load_mailbox(db: Session, address: str, client_id: int | None = None) -> Mailbox:
    mailbox = mailboxes.by_address(db, address)
    if mailbox is None or (client_id is not None and mailbox.client_id != client_id):
        raise NotFound(f"Ящик {address} не зарегистрирован")
    return mailbox


def load_attachment_file(
    db: Session, attachment_id: int, client_id: int | None = None
) -> tuple[Attachment, Path]:
    """Вложение вместе с проверенным путём к файлу.

    Файл мог быть удалён ротацией или лежать вне хранилища — наружу это в любом
    случае «не найдено», без подробностей о файловой системе. Владение
    проверяется через письмо, которому принадлежит вложение (`load_message`) —
    одна и та же проверка, а не вторая копия.
    """
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise NotFound("Вложение не найдено")
    load_message(db, attachment.message_id_fk, client_id)
    path = storage.readable(attachment.filepath)
    if path is None:
        raise NotFound("Вложение не найдено")
    return attachment, path


def attachment_response(path: Path, filename: str, media_type: str) -> Response:
    """Файл хранилища на скачивание — с расшифровкой, если он зашифрован.

    Всегда `Content-Disposition: attachment`: открытие HTML/SVG из письма
    браузером с нашего происхождения было бы XSS. Для зашифрованного файла
    отдаётся поток расшифрованных кусков, а не файл с диска.
    """
    disposition = f"attachment; filename*=utf-8''{quote(filename)}"
    if crypto.is_encrypted_file(path):
        return StreamingResponse(
            storage.stream(path),
            media_type=media_type,
            headers={"Content-Disposition": disposition},
        )
    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        content_disposition_type="attachment",
    )
