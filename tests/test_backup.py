"""Резервное копирование базы и его настройка из панели."""

import sqlite3

import pytest

from app.api.errors import ValidationError
from app.config import settings
from app.services import app_settings, backup
from app.workers import maintenance
from tests.conftest import make_mailbox


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_path", tmp_path / "backups", raising=False)
    return tmp_path / "backups"


# --- Снимок -------------------------------------------------------------------- #


def test_snapshot_is_a_readable_database(db, backup_dir):
    """Копия обязана открываться и содержать данные, а не просто существовать."""
    make_mailbox(db, address="sales@test-gw.kz", is_active=True)
    db.commit()

    snapshot = backup.create()

    assert snapshot.path.is_file()
    with sqlite3.connect(snapshot.path) as copy:
        rows = copy.execute("SELECT address FROM mailbox").fetchall()
    assert rows == [("sales@test-gw.kz",)]


def test_snapshot_captures_wal_contents(db, backup_dir):
    """База работает в режиме WAL.

    Простое копирование файла `.db` дало бы копию без незавершённых
    транзакций из `-wal` — то есть потерю свежих писем, о которой узнаёшь
    только при восстановлении.
    """
    make_mailbox(db, address="fresh@test-gw.kz", is_active=True)
    db.commit()

    with sqlite3.connect(backup.create().path) as copy:
        found = copy.execute(
            "SELECT COUNT(*) FROM mailbox WHERE address = 'fresh@test-gw.kz'"
        ).fetchone()
    assert found == (1,)


def test_snapshots_are_listed_newest_first(db, backup_dir):
    import time

    first = backup.create()
    time.sleep(1.1)  # метка времени в имени — посекундная
    second = backup.create()

    names = [item.name for item in backup.existing()]
    assert names == [second.name, first.name]


# --- Лимит объёма --------------------------------------------------------------- #


def test_old_snapshots_removed_when_over_limit(db, backup_dir):
    import time

    for _ in range(3):
        backup.create()
        time.sleep(1.1)

    newest = backup.existing()[0]
    backup.prune(max_total_bytes=newest.size + 1)

    remaining = backup.existing()
    assert len(remaining) == 1
    assert remaining[0].name == newest.name


def test_newest_snapshot_survives_an_impossible_limit(db, backup_dir, caplog):
    """Лимит ограничивает место, а не лишает копий вовсе."""
    backup.create()

    backup.prune(max_total_bytes=1)

    assert len(backup.existing()) == 1
    assert "оставлен" in caplog.text


def test_prune_on_empty_directory_is_harmless(backup_dir):
    assert backup.prune(max_total_bytes=1024) == 0


# --- Расписание ------------------------------------------------------------------ #


def test_backup_runs_when_none_exists(db, backup_dir):
    assert maintenance.run_backup_if_due() is True
    assert len(backup.existing()) == 1


def test_backup_skipped_while_interval_has_not_passed(db, backup_dir):
    maintenance.run_backup_if_due()
    assert maintenance.run_backup_if_due() is False
    assert len(backup.existing()) == 1


def test_disabled_backup_does_nothing(db, backup_dir):
    app_settings.set_value(db, app_settings.BACKUP_ENABLED, "false")
    db.commit()

    assert maintenance.run_backup_if_due() is False
    assert backup.existing() == []


def test_schedule_survives_restart(db, backup_dir):
    """Расписание считается по файлам на диске, а не по таймеру в памяти.

    Иначе частые перезапуски сервиса заставляли бы делать копию каждый раз.
    """
    maintenance.run_backup_if_due()
    app_settings.set_value(db, app_settings.BACKUP_INTERVAL_HOURS, "24")
    db.commit()

    assert maintenance.run_backup_if_due() is False


# --- Настройки из панели ---------------------------------------------------------- #


def test_defaults_apply_without_any_record(db):
    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 1024
    assert app_settings.get(db, app_settings.BACKUP_ENABLED) is True


def test_value_round_trips(db):
    app_settings.set_value(db, app_settings.BACKUP_MAX_TOTAL_MB, "2048")
    db.commit()
    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 2048


@pytest.mark.parametrize("bad", ["0", "-5", "не число", "", "99999999"])
def test_out_of_range_values_are_refused(db, bad):
    with pytest.raises(ValidationError):
        app_settings.set_value(db, app_settings.BACKUP_MAX_TOTAL_MB, bad)


def test_corrupt_stored_value_falls_back_to_default(db):
    """Испорченная запись не должна останавливать резервное копирование."""
    from app.models import AppSetting

    db.add(AppSetting(key=app_settings.BACKUP_MAX_TOTAL_MB.key, value="мусор"))
    db.commit()

    assert app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB) == 1024


def test_unchecked_box_turns_the_setting_off(db):
    """Снятый флажок в теле формы не приходит вовсе."""
    app_settings.apply_form(db, {"backup.interval_hours": "12"})
    db.commit()

    assert app_settings.get(db, app_settings.BACKUP_ENABLED) is False
    assert app_settings.get(db, app_settings.BACKUP_INTERVAL_HOURS) == 12
