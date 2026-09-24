"""Файловое хранилище вложений — единственное место, где что-либо пишется на диск.

Раньше раскладка каталогов, очистка имени и запись байтов дублировались в
загрузке через API и в приёме почты по SMTP; расходились и правила, и лимиты.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from .. import crypto
from ..config import settings
from ..db import utcnow
from ..utils import is_within, sanitize_filename, unique_filename

logger = logging.getLogger(__name__)

OUTGOING_DIR = "outgoing"


def root() -> Path:
    return settings.attachment_path


def store(data: bytes, filename: str, *parts: str) -> Path:
    """Кладёт байты в `<корень>/<parts...>/<уникальное имя>` и возвращает путь.

    Части пути тоже очищаются: элементы вроде адреса ящика приходят из письма
    и не могут считаться доверенными.
    """
    path = open_target(filename, *parts)
    crypto.write_file(path, data)
    return path


def open_target(filename: str, *parts: str) -> Path:
    """Готовит (но не создаёт) уникальный путь внутри хранилища."""
    directory = root().joinpath(*(sanitize_filename(p) for p in parts))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / unique_filename(filename)
    if not is_within(path, root()):
        raise ValueError(f"Путь вложения вне хранилища: {path}")
    return path


def store_incoming(data: bytes, filename: str, mailbox: str, message_id: int) -> Path:
    """Раскладка входящих: <ящик>/<ГГГГ-ММ-ДД>/<id письма>/<файл> (F-08)."""
    return store(data, filename, mailbox, utcnow().strftime("%Y-%m-%d"), str(message_id))


def store_outgoing(data: bytes, filename: str) -> Path:
    return store(data, filename, OUTGOING_DIR, utcnow().strftime("%Y-%m-%d"))


def store_outgoing_stream(filename: str) -> Path:
    """Путь под потоковую запись исходящего вложения — писать будет вызывающий."""
    return open_target(filename, OUTGOING_DIR, utcnow().strftime("%Y-%m-%d"))


def open_sink(path: Path):
    """Запись потоком (загрузки): шифрует при заданном ключе.

    Контекстный менеджер с объектом, у которого есть `write(bytes)`.
    """
    return crypto.open_sink(path)


def read(path: Path) -> bytes:
    """Содержимое файла хранилища открытым текстом, независимо от того,
    зашифрован он или записан до включения шифрования."""
    return crypto.read_file(path)


def stream(path: Path) -> Iterator[bytes]:
    """То же, что `read`, но кусками — для отдачи больших файлов по HTTP."""
    return crypto.iter_file(path)


def readable(filepath: str) -> Path | None:
    """Путь, пригодный для отдачи клиенту, либо None.

    Отдаём только файлы внутри хранилища, даже если в БД оказалась строка,
    указывающая наружу.
    """
    path = Path(filepath)
    if not path.is_file() or not is_within(path, root()):
        return None
    return path


def remove(filepath: str) -> None:
    path = Path(filepath)
    if not is_within(path, root()):
        logger.warning("Отказ удалять файл вне хранилища: %s", filepath)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Не удалось удалить %s: %s", filepath, exc)


def purge_empty_dirs(older_than: timedelta = timedelta(days=1)) -> None:
    """Убирает пустые каталоги, оставшиеся после чистки загрузок.

    Обход от самых глубоких к верхним, иначе освободившийся родитель на этом же
    проходе останется непустым.
    """
    cutoff = (utcnow() - older_than).timestamp()
    for directory in sorted(root().rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if not directory.is_dir() or any(directory.iterdir()):
                continue
            if directory.stat().st_mtime > cutoff:
                continue  # каталог могли создать только что под новую загрузку
            directory.rmdir()
        except OSError:
            continue
