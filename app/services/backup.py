"""Резервное копирование базы данных.

Копируется база — то есть письма, ящики, учётные записи и настройки. Вложения
в снимок не входят намеренно: это файлы, которые пишутся один раз и больше не
меняются, и складывать их копию в каждый снимок значило бы умножать гигабайты
без всякой пользы. Их достаточно синхронизировать отдельно (`robocopy /MIR`),
о чём сказано в документации.
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from ..config import settings
from ..db import engine, utcnow

logger = logging.getLogger(__name__)

PREFIX = "mail_gateway-"
SUFFIX = ".db"
_STAMP = "%Y%m%d-%H%M%S"


@dataclass(frozen=True)
class Snapshot:
    path: Path
    size: int
    created_at: datetime

    @property
    def name(self) -> str:
        return self.path.name


class BackupError(Exception):
    """Снимок сделать не удалось."""


def directory() -> Path:
    path = settings.backup_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def create() -> Snapshot:
    """Делает согласованный снимок базы и возвращает его описание.

    `VACUUM INTO` вместо копирования файла: база работает в режиме WAL, и
    простое копирование `.db` даёт файл без незавершённых транзакций из
    `-wal` — то есть повреждённую копию, о чём узнаёшь в момент
    восстановления. SQLite делает снимок сам, не останавливая запись.
    """
    if not settings.is_sqlite:
        raise BackupError(
            "Автоматический снимок реализован только для SQLite. "
            "Для другой СУБД пользуйтесь её штатными средствами."
        )

    target = directory() / f"{PREFIX}{utcnow().strftime(_STAMP)}{SUFFIX}"
    if target.exists():
        raise BackupError(f"Снимок {target.name} уже существует")

    try:
        with engine.connect() as conn:
            # Путь подставляется в текст запроса, потому что VACUUM INTO не
            # принимает параметров. Имя формируется здесь же из метки времени
            # и наружу не выходит, но кавычки всё равно экранируем.
            literal = str(target).replace("'", "''")
            conn.execute(text(f"VACUUM INTO '{literal}'"))
    except Exception as exc:  # наружу одним типом
        # Недописанный файл убираем: иначе он попадёт в список снимков и
        # будет выглядеть годным для восстановления.
        target.unlink(missing_ok=True)
        raise BackupError(f"Не удалось создать снимок: {exc}") from exc

    snapshot = _describe(target)
    logger.info("Создан снимок базы: %s (%s КБ)", snapshot.name, snapshot.size // 1024)
    return snapshot


def existing() -> list[Snapshot]:
    """Снимки от новых к старым."""
    path = settings.backup_path
    if not path.is_dir():
        return []
    found = [
        _describe(item)
        for item in path.glob(f"{PREFIX}*{SUFFIX}")
        if item.is_file()
    ]
    return sorted(found, key=lambda s: s.created_at, reverse=True)


def total_size() -> int:
    return sum(item.size for item in existing())


def prune(max_total_bytes: int) -> int:
    """Удаляет старые снимки, пока сумма не уложится в лимит. Возвращает число удалённых.

    Самый свежий снимок не удаляется никогда, даже если он один превышает
    лимит: смысл лимита — ограничить место, а не остаться без копий вовсе.
    Если так вышло — это повод увеличить лимит, и об этом пишется в журнал.
    """
    snapshots = existing()
    if not snapshots:
        return 0

    keep_newest, rest = snapshots[0], snapshots[1:]
    used = keep_newest.size
    removed = 0

    for snapshot in rest:
        if used + snapshot.size <= max_total_bytes:
            used += snapshot.size
            continue
        try:
            snapshot.path.unlink()
            removed += 1
            logger.info("Удалён старый снимок %s", snapshot.name)
        except OSError as exc:
            logger.warning("Не удалось удалить снимок %s: %s", snapshot.name, exc)
            used += snapshot.size

    if keep_newest.size > max_total_bytes:
        logger.warning(
            "Снимок %s (%s МБ) один превышает лимит %s МБ — оставлен, "
            "иначе резервных копий не осталось бы вовсе",
            keep_newest.name, keep_newest.size // 1024 // 1024,
            max_total_bytes // 1024 // 1024,
        )
    return removed


def free_space() -> int | None:
    """Свободное место на диске со снимками, либо None если узнать не удалось."""
    try:
        return shutil.disk_usage(directory()).free
    except OSError:
        return None


def _describe(path: Path) -> Snapshot:
    stat = path.stat()
    return Snapshot(
        path=path,
        size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
    )
