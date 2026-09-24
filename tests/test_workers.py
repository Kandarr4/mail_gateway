"""Очередь отправки: повторы, исчерпание попыток, восстановление (F-16…F-21).

Проверяется каркас `QueueWorker`: захват задачи, backoff и запись результата.
"""

import asyncio
import threading

import pytest
from sqlalchemy import select

from app.config import settings
from app.constants import MessageStatus
from app.db import utcnow
from app.models import Message
from app.services import messages as messages_service
from app.services.delivery import PermanentSendError
from app.workers.outbox import OutboxWorker


def queued_message(db, **overrides) -> Message:
    fields = {
        "direction": "outgoing",
        "status": MessageStatus.QUEUED,
        "sender": "sales@test-gw.kz",
        "recipient": "client@outside.example",
        "subject": "Тема",
        "body_text": "Текст",
        "message_id": "<out-1@test-gw.kz>",
        "next_attempt_at": utcnow(),
    }
    message = Message(**{**fields, **overrides})
    db.add(message)
    db.commit()
    return message


async def run_one(worker, stop: asyncio.Event | None = None) -> None:
    """Прогоняет ровно один цикл воркера."""
    await worker._tick(stop or asyncio.Event())


# --- Повторы и исчерпание попыток ------------------------------------------ #


@pytest.mark.asyncio
async def test_transient_failure_is_retried_with_backoff(db, monkeypatch):
    message = queued_message(db)
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _raise(ConnectionError("MX недоступен")))

    await run_one(worker)

    db.expire_all()
    refreshed = db.get(Message, message.id)
    assert refreshed.status == MessageStatus.QUEUED
    assert refreshed.attempts == 1
    assert "MX недоступен" in refreshed.last_error
    assert refreshed.next_attempt_at > utcnow()


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(db, monkeypatch):
    message = queued_message(db)
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _raise(PermanentSendError("нет MX-записей")))

    await run_one(worker)

    db.expire_all()
    refreshed = db.get(Message, message.id)
    assert refreshed.status == MessageStatus.FAILED
    assert refreshed.attempts == 1  # повторов не было


@pytest.mark.asyncio
async def test_attempts_are_exhausted(db, monkeypatch):
    message = queued_message(db, attempts=settings.send_max_attempts - 1)
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _raise(ConnectionError("снова сбой")))

    await run_one(worker)

    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.FAILED


@pytest.mark.asyncio
async def test_success_marks_sent_and_stamps_time(db, monkeypatch):
    message = queued_message(db)
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _noop())

    await run_one(worker)

    db.expire_all()
    refreshed = db.get(Message, message.id)
    assert refreshed.status == MessageStatus.SENT
    assert refreshed.sent_at is not None
    assert refreshed.last_error is None


@pytest.mark.asyncio
async def test_not_due_message_is_not_claimed(db, monkeypatch):
    from datetime import timedelta

    message = queued_message(db, next_attempt_at=utcnow() + timedelta(hours=1))
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _raise(AssertionError("не должно вызываться")))

    await run_one(worker)

    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.QUEUED


@pytest.mark.asyncio
async def test_incoming_message_is_never_sent(db, monkeypatch):
    """Воркер отправки обязан видеть только исходящие."""
    message = queued_message(db, direction="incoming")
    worker = OutboxWorker()
    monkeypatch.setattr(worker, "handle", _raise(AssertionError("не должно вызываться")))

    await run_one(worker)

    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.QUEUED


# --- Захват задачи ----------------------------------------------------------- #


def test_concurrent_claims_never_hand_out_the_same_message(db):
    """Двойной захват — это письмо, доставленное получателю дважды.

    Читать строку, менять её в Python и коммитить — гонка: между чтением и
    записью в неё успевает вклиниться соседний поток. Проверяем то, что от
    захвата действительно требуется, а не то, как он устроен.
    """
    total = 8
    for i in range(total):
        queued_message(db, message_id=f"<race-{i}@test-gw.kz>")

    worker = OutboxWorker()
    claimed: list[int] = []
    failures: list[Exception] = []
    lock = threading.Lock()

    def claim_once():
        try:
            job = worker._claim()
        except Exception as exc:  # noqa: BLE001 — падение потока тоже результат
            with lock:
                failures.append(exc)
            return
        if job is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=claim_once) for _ in range(total * 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"захват сорвался: {failures[0]!r}"
    assert len(claimed) == len(set(claimed)), "одно письмо захвачено дважды"

    db.expire_all()
    in_progress = [m for m in db.scalars(select(Message)) if m.status == MessageStatus.SENDING]
    assert len(in_progress) == len(claimed)
    # Счётчик увеличен ровно на единицу — значит, считала БД, а не Python
    # по устаревшему значению.
    assert all(m.attempts == 1 for m in in_progress)


@pytest.mark.asyncio
async def test_messages_are_delivered_in_parallel(db, monkeypatch):
    """Одна линия обработки означает, что вся очередь стоит за одним письмом.

    Барьер проходится только вчетвером: при последовательной отправке первый
    же обработчик встанет здесь до таймаута, и письма не уйдут.
    """
    monkeypatch.setattr(settings, "send_concurrency", 4)
    ids = [queued_message(db, message_id=f"<par-{i}@test-gw.kz>").id for i in range(4)]

    worker = OutboxWorker()
    barrier = asyncio.Barrier(4)

    async def handle(_job):
        await asyncio.wait_for(barrier.wait(), timeout=5)

    monkeypatch.setattr(worker, "handle", handle)

    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            db.expire_all()
            if all(db.get(Message, i).status == MessageStatus.SENT for i in ids):
                break
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    db.expire_all()
    assert [db.get(Message, i).status for i in ids] == [MessageStatus.SENT] * 4


# --- Пауза перед повтором ----------------------------------------------------- #


def test_backoff_is_capped():
    """Чистая экспонента уводит последние попытки за срок жизни письма."""
    message = Message(direction="outgoing", status=MessageStatus.QUEUED,
                      sender="a@test-gw.kz", recipient="b@example.org", attempts=20)

    delay = message.schedule_retry("сбой", base_seconds=60, max_seconds=3600)

    assert 3600 * 0.8 <= delay <= 3600 * 1.2


def test_backoff_is_spread_out():
    """Без разброса письма, отложенные одной аварией, вернутся одной пачкой."""
    message = Message(direction="outgoing", status=MessageStatus.QUEUED,
                      sender="a@test-gw.kz", recipient="b@example.org", attempts=1)

    delays = {message.schedule_retry("сбой", 600, 3600) for _ in range(20)}

    assert len(delays) > 1


# --- Восстановление после аварийной остановки ------------------------------- #


def test_stuck_sending_messages_are_requeued(db):
    """Письмо, застрявшее в `sending`, иначе не подхватит ни один воркер."""
    message = queued_message(db, status=MessageStatus.SENDING, attempts=1)

    messages_service.requeue_stuck_sending(db)
    db.commit()

    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.QUEUED


def test_stuck_message_with_exhausted_attempts_fails(db):
    message = queued_message(db, status=MessageStatus.SENDING, attempts=settings.send_max_attempts)

    messages_service.requeue_stuck_sending(db)
    db.commit()

    db.expire_all()
    assert db.get(Message, message.id).status == MessageStatus.FAILED


def _raise(exc: Exception):
    async def _handler(_job):
        raise exc

    return _handler


def _noop():
    async def _handler(_job):
        return None

    return _handler
