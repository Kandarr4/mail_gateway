"""Периодическое обслуживание: чистка брошенных загрузок и резервные копии."""

import asyncio
import logging
from datetime import timedelta

from ..db import session_scope, utcnow
from ..services import app_settings, backup, uploads

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 3600


async def maintenance_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        for step in (uploads.purge_expired, run_backup_if_due):
            try:
                await asyncio.to_thread(step)
            except Exception:
                # Сбой одного дела не должен отменять остальные: не сделанная
                # копия — не повод перестать чистить загрузки, и наоборот.
                logger.exception("Ошибка планового обслуживания: %s", step.__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_SECONDS)
        except TimeoutError:
            continue


def run_backup_if_due() -> bool:
    """Делает снимок, если с прошлого прошло достаточно времени. True — сделан.

    Расписание считается по времени последнего снимка на диске, а не по
    таймеру в памяти: после перезапуска сервиса таймер обнулился бы, и при
    частых перезапусках копии делались бы каждый раз заново.
    """
    with session_scope() as db:
        if not app_settings.get(db, app_settings.BACKUP_ENABLED):
            return False
        interval = app_settings.get(db, app_settings.BACKUP_INTERVAL_HOURS)
        limit_mb = app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB)

    if not _is_due(int(interval)):
        return False

    backup.create()
    backup.prune(int(limit_mb) * 1024 * 1024)
    return True


def _is_due(interval_hours: int) -> bool:
    snapshots = backup.existing()
    if not snapshots:
        return True
    age = utcnow() - snapshots[0].created_at
    return age >= timedelta(hours=interval_hours)
