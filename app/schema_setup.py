"""Приведение схемы БД к актуальной версии.

Схема версионируется Alembic — единственный источник правды о структуре.
`create_all()` намеренно не используется: иначе на боевой базе, созданной
прошлой версией, недостающие колонки не появятся, а расхождение обнаружится
только в момент падения запроса.

Миграции накатываются под межпроцессной блокировкой: службу и окно программы
запускают одновременно, и оба зовут `prepare_database` (см. `_schema_lock`).
"""

import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import BASE_DIR, settings

logger = logging.getLogger(__name__)

#: Сколько ждать чужую миграцию, прежде чем идти напролом. С запасом: на
#: пустой базе накатывается вся история, на медленном диске это секунды.
LOCK_TIMEOUT_SECONDS = 120

# Миграции — часть программы, не развёртывания: в сборке они лежат в
# `_internal` (см. mail_gateway.spec), а не рядом с exe, как `.env` и данные.
if getattr(sys, "frozen", False):
    MIGRATIONS_DIR = Path(sys._MEIPASS) / "migrations"
else:
    MIGRATIONS_DIR = BASE_DIR / "migrations"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url.replace("%", "%%"))
    return config


def prepare_database() -> None:
    """Накатывает миграции и готовит каталоги хранилища."""
    _ensure_sqlite_dir()
    with _schema_lock():
        command.upgrade(alembic_config(), "head")
    settings.attachment_path.mkdir(parents=True, exist_ok=True)
    logger.info("Схема БД актуальна")


def _ensure_sqlite_dir() -> None:
    """Каталог для файла SQLite должен существовать до первого подключения."""
    sqlite_file = _sqlite_path()
    if sqlite_file is not None:
        sqlite_file.parent.mkdir(parents=True, exist_ok=True)


def _sqlite_path() -> Path | None:
    prefix = "sqlite:///"
    url = settings.sqlalchemy_url
    return Path(url[len(prefix):]) if url.startswith(prefix) else None


# --- Межпроцессная блокировка миграций --------------------------------------- #

if os.name == "nt":
    import msvcrt

    def _try_lock(handle: int) -> None:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)

    def _unlock(handle: int) -> None:
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover — бой только под Windows, ветка для разработки
    import fcntl

    def _try_lock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def _lock_path() -> Path:
    """Файл блокировки рядом с базой: одна база — одна очередь на миграцию."""
    sqlite_file = _sqlite_path()
    return (sqlite_file.parent if sqlite_file else BASE_DIR) / "schema.lock"


@contextlib.contextmanager
def _schema_lock():
    """Пропускает к миграциям по одному процессу.

    Служба и окно программы стартуют одновременно: `run.bat` запускает их
    парой, а в сборке служба поднимается при загрузке сервера, окно — при
    входе оператора. Оба зовут `prepare_database`, и Alembic в каждом читает
    текущую версию, составляет план и выполняет его. Пока второй составлял
    план, первый успевал применить те же миграции — и второй падал на
    «table already exists» или «duplicate column name: spf_result». Гонка
    воспроизводится стабильно: из четырёх одновременных стартов падали трое.

    Блокировка файловая, а не именованный мьютекс Windows: снимается
    операционной системой при смерти процесса и не требует прав на глобальное
    пространство имён — служба работает в сеансе 0, окно в пользовательском,
    и `Local\\`-мьютекс их бы не связал.

    Не дождались — идём накатывать всё равно: не подняться из-за чужой
    зависшей блокировки хуже, чем рискнуть гонкой, которой может и не быть.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT)
    locked = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_lock(handle)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Схема БД занята другим процессом дольше %s с — "
                        "обновляю без блокировки",
                        LOCK_TIMEOUT_SECONDS,
                    )
                    break
                time.sleep(0.2)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _unlock(handle)
        os.close(handle)
