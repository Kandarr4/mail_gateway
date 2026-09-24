"""Фоновые воркеры шлюза."""

import asyncio

from .maintenance import maintenance_worker
from .outbox import OutboxWorker

__all__ = ["OutboxWorker", "maintenance_worker", "start_all"]


def start_all(stop: asyncio.Event) -> list[asyncio.Task]:
    """Поднимает все фоновые задачи процесса."""
    return [
        asyncio.create_task(OutboxWorker().run(stop), name="outbox-worker"),
        asyncio.create_task(maintenance_worker(stop), name="maintenance-worker"),
    ]
