"""Подготовка схемы БД при одновременном старте нескольких процессов.

Служба и окно программы поднимаются парой: `run.bat` запускает их вместе, а
в сборке служба стартует при загрузке сервера, окно — при входе оператора.
Оба зовут `prepare_database`, и без межпроцессной блокировки Alembic в одном
из них падает на «table already exists» или «duplicate column name»: план
миграции составлен по версии, которую сосед уже успел обновить.

Тест поднимает настоящие процессы, а не потоки: гонка межпроцессная, и
внутри одного интерпретатора её не воспроизвести.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Ровно то, что делает каждый из стартующих процессов.
_MIGRATE = (
    "import sys; sys.path.insert(0, r'{root}');"
    "from app.schema_setup import prepare_database; prepare_database()"
)

WORKERS = 4


@pytest.fixture
def fresh_db_env(tmp_path):
    """Окружение с собственной пустой базой — накатывается вся история миграций."""
    import os

    env = dict(os.environ)
    env.update(
        MG_DOMAIN="race.kz",
        MG_API_TOKENS="token",
        MG_DATABASE_URL=f"sqlite:///{tmp_path / 'race.db'}",
        MG_ATTACHMENT_DIR=str(tmp_path / "attachments"),
        MG_LOG_DIR=str(tmp_path / "logs"),
        PYTHONUTF8="1",
    )
    return env


def test_simultaneous_startup_migrates_once(fresh_db_env):
    command = [sys.executable, "-c", _MIGRATE.format(root=ROOT)]
    processes = [
        subprocess.Popen(command, env=fresh_db_env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace")
        for _ in range(WORKERS)
    ]
    results = [(process.wait(timeout=180), process.stdout.read()) for process in processes]

    broken = [output for code, output in results if code != 0]
    assert not broken, "одновременный старт сломал миграции:\n" + "\n---\n".join(broken)


def test_schema_lock_lives_next_to_the_database(monkeypatch, tmp_path):
    """Блокировка привязана к базе, а не к программе: два развёртывания на
    одной машине не должны ждать друг друга."""
    from app import schema_setup

    monkeypatch.setattr(
        schema_setup.settings, "sqlalchemy_url", f"sqlite:///{tmp_path / 'own.db'}"
    )
    assert schema_setup._lock_path().parent == tmp_path
