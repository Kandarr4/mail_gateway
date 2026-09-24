"""Инфраструктура доступа к данным: движок, сессии, базовый класс, типы.

Здесь нет ни моделей (см. `models.py`), ни бизнес-логики.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, Text, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import crypto
from .config import settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Текущий момент в UTC. Единственный источник времени в приложении."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Datetime, который всегда остаётся aware-UTC по обе стороны БД.

    SQLite молча теряет tzinfo при чтении: записав aware-время, обратно
    получаешь naive. Это приводило к тому, что `created_at` уезжал в API и
    вебхуки без пометки зоны, и потребитель трактовал UTC как локальное время.
    Здесь нормализация делается один раз для всех моделей.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class EncryptedText(TypeDecorator):
    """Текст, который в БД лежит зашифрованным, а в коде остаётся строкой.

    Шифрование прозрачно для всего приложения — API, панели, воркеров: ORM
    отдаёт открытый текст, как обычная колонка `Text`. Без ключа
    (`MG_ENCRYPTION_KEY_FILE` пуст) значения проходят насквозь; смешанное
    состояние читаемо в обе стороны — зашифрованные строки несут префикс
    (см. `crypto.decrypt_text`).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect):
        if value is None:
            return None
        return crypto.encrypt_text(value)

    def process_result_value(self, value: str | None, dialect):
        if value is None:
            return None
        return crypto.decrypt_text(value)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}  # noqa: RUF012 — контракт DeclarativeBase из SQLAlchemy


def _create_engine() -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if settings.is_sqlite:
        # Соединение шарится между потоками (SMTP-поток, воркеры, HTTP).
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(settings.sqlalchemy_url, **kwargs)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - инфраструктура
        """WAL и внешние ключи: без них конкурентная запись из воркеров и
        SMTP-потока упирается в `database is locked`, а cascade-удаление
        вложений не выполняется."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Единица работы вне HTTP (воркеры, SMTP): commit при успехе, rollback при ошибке.

    HTTP-роуты используют не это, а зависимость `get_db` + декоратор `@endpoint`.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> bool:
    """Проверка живости БД для /ready."""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("БД недоступна")
        return False
