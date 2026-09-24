"""Каркас фонового воркера очереди с повторными попытками.

Очередь писем и очередь вебхуков различаются только тем, *что* делается с
задачей. Цикл опроса, захват задачи, backoff, исчерпание попыток и запись
результата — общие и живут здесь в единственном экземпляре.
"""

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import WORKER_IDLE_SECONDS
from ..db import session_scope, utcnow

logger = logging.getLogger(__name__)

JobT = TypeVar("JobT", bound="Job")


@dataclass(frozen=True)
class Job:
    """Снимок задачи, отвязанный от сессии: воркер работает без открытой транзакции."""

    id: int


class QueueWorker(ABC, Generic[JobT]):
    name: str
    model: type
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    idle_seconds: float = WORKER_IDLE_SECONDS
    #: Сколько задач воркер обрабатывает одновременно.
    concurrency: int = 1

    #: Исключения, при которых повторять бессмысленно — задача сразу в failed.
    permanent_errors: tuple[type[Exception], ...] = ()

    #: Воркер на паузе (см. `pause_reason`). Хранится на экземпляре, общем для
    #: всех линий обработки, — переход в журнал попадает один раз, а не по
    #: разу на линию.
    _paused: bool = False

    # --- реализуют наследники ------------------------------------------- #

    @abstractmethod
    def extract(self, row) -> JobT:
        """Переносит нужные поля из строки БД в неизменяемый снимок."""

    @abstractmethod
    async def handle(self, job: JobT) -> None:
        """Выполняет задачу. Исключение = неуспех, дальше решает каркас."""

    def after_finish(self, db: Session, row, status: str, error: str | None) -> None:
        """Побочные эффекты в той же транзакции, что и смена статуса."""

    async def prepare(self) -> None:
        """Подготовка ресурсов перед циклом (клиенты, соединения)."""

    async def cleanup(self) -> None:
        """Освобождение ресурсов после остановки."""

    def enabled(self) -> bool:
        return True

    def pause_reason(self) -> str:
        """Почему воркер сейчас не берёт задачи. Пусто — работает.

        В отличие от `enabled`, спрашивается на каждом обороте: причина может
        появиться и исчезнуть на ходу (например, истёкшая и продлённая
        лицензия), и воркер сам возобновит работу, не требуя перезапуска.
        """
        return ""

    # --- каркас ---------------------------------------------------------- #

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled():
            logger.info("Воркер %s выключен настройками", self.name)
            return

        await self.prepare()
        logger.info("Воркер %s запущен, параллельных потоков: %s", self.name, self.concurrency)
        try:
            await asyncio.gather(*(self._loop(stop) for _ in range(self.concurrency)))
        except asyncio.CancelledError:
            raise
        finally:
            await self.cleanup()
            logger.info("Воркер %s остановлен", self.name)

    async def _loop(self, stop: asyncio.Event) -> None:
        """Одна независимая линия обработки.

        Их несколько, потому что задача занимает поток целиком: SMTP-сессия с
        недоступным сервером висит до таймаута в полминуты, и при единственной
        линии вся очередь стоит за одним письмом к одному сломанному адресату.
        """
        while not stop.is_set():
            await self._tick(stop)

    async def _tick(self, stop: asyncio.Event) -> None:
        if await asyncio.to_thread(self._on_pause_checked):
            await self._idle(stop)
            return

        try:
            job = await asyncio.to_thread(self._claim)
        except Exception:
            logger.exception("Воркер %s: не удалось получить задачу", self.name)
            await self._idle(stop)
            return

        if job is None:
            await self._idle(stop)
            return

        error: Exception | None = None
        try:
            await self.handle(job)
        except asyncio.CancelledError:
            # Остановка процесса — вернём задачу в очередь, а не в failed.
            await asyncio.to_thread(self._release, job.id)
            raise
        except Exception as exc:  # noqa: BLE001 — решение о повторе ниже
            error = exc

        try:
            await asyncio.to_thread(self._finish, job.id, error)
        except Exception:
            logger.exception("Воркер %s: не удалось записать результат задачи %s", self.name, job.id)

    def _on_pause_checked(self) -> bool:
        """True — задачи сейчас не берём. Заодно пишет в журнал переходы.

        Именно переходы, а не каждую проверку: воркер опрашивает очередь
        секундами, и «пауза» в журнале на каждом обороте залила бы его
        целиком, скрыв всё остальное.
        """
        reason = self.pause_reason()
        if reason and not self._paused:
            logger.warning("Воркер %s приостановлен. %s", self.name, reason)
        elif not reason and self._paused:
            logger.info("Воркер %s возобновил работу", self.name)
        self._paused = bool(reason)
        return self._paused

    async def _idle(self, stop: asyncio.Event) -> None:
        """Пауза, прерываемая сигналом остановки — без «залипания» на shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self.idle_seconds)

    def _claim(self) -> JobT | None:
        """Атомарно забирает следующую созревшую задачу.

        Захват — двумя операторами, и второй из них решает всё. Выбрать строку,
        поменять её в Python и закоммитить было бы гонкой: между чтением и
        записью другой поток (а при нескольких экземплярах шлюза — другой
        процесс) успевает прочитать ту же строку, и письмо уходит получателю
        дважды. Поэтому смена статуса идёт условным `UPDATE ... WHERE
        status = PENDING`: его выполняет БД, и выигрывает ровно один
        претендент — остальные видят `rowcount = 0` и уходят за следующей
        задачей.
        """
        with session_scope() as db:
            stmt = (
                select(self.model.id)
                .where(
                    self.model.status == self.model.PENDING,
                    self.model.next_attempt_at <= utcnow(),
                )
                .order_by(self.model.next_attempt_at, self.model.id)
                # Не одна строка, а несколько: все линии обработки просыпаются
                # одновременно и целятся в самую старую задачу. Победитель
                # один, а остальные с единственным кандидатом ушли бы в паузу
                # на `idle_seconds`, хотя очередь полна работы. Со списком
                # проигравший сразу переходит к следующей задаче.
                .limit(max(2 * self.concurrency, 8))
            )
            stmt = self.narrow(stmt)
            if not settings.is_sqlite:
                # На Postgres соседний экземпляр не будет ждать на этих же строках.
                stmt = stmt.with_for_update(skip_locked=True)

            for job_id in db.scalars(stmt):
                claimed = db.execute(
                    update(self.model)
                    .where(self.model.id == job_id, self.model.status == self.model.PENDING)
                    .values(**self.model.claim_values())
                ).rowcount
                if claimed:
                    row = db.get(self.model, job_id)
                    return self.extract(row) if row is not None else None
            return None

    def narrow(self, stmt):
        """Дополнительное условие выборки (например, направление письма)."""
        return stmt

    def _release(self, job_id: int) -> None:
        with session_scope() as db:
            row = db.get(self.model, job_id)
            if row is not None:
                row.status = self.model.PENDING
                row.next_attempt_at = utcnow()

    def _finish(self, job_id: int, error: Exception | None) -> str:
        """Единственное место, где решается «повторить или признать провал»."""
        with session_scope() as db:
            row = db.get(self.model, job_id)
            if row is None:
                return self.model.FAILED

            if error is None:
                row.mark_done()
                logger.info("Воркер %s: задача %s выполнена", self.name, job_id)
            elif isinstance(error, self.permanent_errors) or row.attempts_exhausted(self.max_attempts):
                row.mark_failed(str(error))
                logger.error("Воркер %s: задача %s провалена: %s", self.name, job_id, error)
            else:
                delay = row.schedule_retry(
                    str(error), self.retry_base_seconds, self.retry_max_seconds
                )
                logger.warning(
                    "Воркер %s: задача %s, попытка %s неудачна (%s), повтор через %s с",
                    self.name, job_id, row.attempts, error, delay,
                )

            status = row.status
            self.after_finish(db, row, status, str(error) if error else None)
            return status
