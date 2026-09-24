"""Первый запуск: заведение учётной записи панели с консоли."""

import io

import pytest

from app import bootstrap
from app.config import settings
from app.models import AdminUser
from app.services import admin_users
from app.services.passwords import hash_password

GOOD_PASSWORD = "Pervy1-Parol"


class FakeTTY(io.StringIO):
    """stdin, который считает себя терминалом."""

    def isatty(self):
        return True


@pytest.fixture
def console(monkeypatch):
    """Подставляет ответы на `input()` и `getpass()`."""

    def setup(*, username="", passwords=()):
        monkeypatch.setattr(bootstrap.sys, "stdin", FakeTTY(f"{username}\n"))
        monkeypatch.setattr(bootstrap, "input", lambda _prompt: username, raising=False)
        answers = list(passwords)
        monkeypatch.setattr(
            bootstrap.getpass, "getpass", lambda _prompt="": answers.pop(0) if answers else ""
        )

    return setup


# --- Неинтерактивный запуск ---------------------------------------------------- #


def test_service_start_never_blocks_on_a_prompt(db, monkeypatch, caplog):
    """Под службой Windows stdin не терминал — сервер обязан просто стартовать.

    Диалог здесь означал бы, что почтовый сервер не поднимается вообще, а
    причину видно только тому, кто заглянет в консоль службы.
    """
    monkeypatch.setattr(bootstrap.sys, "stdin", io.StringIO())  # isatty() → False
    monkeypatch.setattr(
        bootstrap.getpass, "getpass", _forbidden("пароль не должен спрашиваться")
    )

    bootstrap.ensure_admin_exists()

    assert admin_users.count(db) == 0
    assert "manage.py create-admin" in caplog.text


def test_closed_stdin_is_not_interactive(monkeypatch):
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr(bootstrap.sys, "stdin", stream)
    assert bootstrap._interactive() is False


def test_missing_stdin_is_not_interactive(monkeypatch):
    """У процесса без консоли `sys.stdin` может быть None."""
    monkeypatch.setattr(bootstrap.sys, "stdin", None)
    assert bootstrap._interactive() is False


# --- Учётная запись уже есть --------------------------------------------------- #


def test_existing_admin_is_not_asked_about(db, monkeypatch):
    db.add(AdminUser(username="operator", password_hash=hash_password(GOOD_PASSWORD)))
    db.commit()

    monkeypatch.setattr(bootstrap.sys, "stdin", FakeTTY())
    monkeypatch.setattr(bootstrap.getpass, "getpass", _forbidden("лишний вопрос о пароле"))

    bootstrap.ensure_admin_exists()

    assert admin_users.count(db) == 1


def test_disabled_panel_needs_no_account(db, monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", False)
    monkeypatch.setattr(bootstrap.getpass, "getpass", _forbidden("панель выключена"))

    bootstrap.ensure_admin_exists()

    assert admin_users.count(db) == 0


# --- Диалог -------------------------------------------------------------------- #


def test_account_created_from_console(db, console):
    console(username="operator", passwords=[GOOD_PASSWORD, GOOD_PASSWORD])

    bootstrap.ensure_admin_exists()

    assert admin_users.by_username(db, "operator") is not None


def test_empty_name_falls_back_to_admin(db, console):
    console(username="", passwords=[GOOD_PASSWORD, GOOD_PASSWORD])

    bootstrap.ensure_admin_exists()

    assert admin_users.by_username(db, bootstrap.DEFAULT_USERNAME) is not None


def test_password_is_asked_twice_and_mismatch_retried(db, console):
    """Опечатка в пароле, введённом вслепую, иначе запирает панель навсегда."""
    console(username="operator", passwords=["Parol-Odin1", "Parol-Dva22",
                                            GOOD_PASSWORD, GOOD_PASSWORD])

    bootstrap.ensure_admin_exists()

    assert admin_users.authenticate(db, "operator", GOOD_PASSWORD) is not None


def test_weak_password_is_refused_then_accepted(db, console):
    console(username="operator", passwords=["123", "123", GOOD_PASSWORD, GOOD_PASSWORD])

    bootstrap.ensure_admin_exists()

    assert admin_users.authenticate(db, "operator", GOOD_PASSWORD) is not None


def test_gives_up_after_repeated_failures(db, console, caplog):
    """Сервер должен подняться даже так — приём почты от панели не зависит."""
    console(username="operator", passwords=["короткий", "короткий"] * bootstrap.MAX_ATTEMPTS)

    bootstrap.ensure_admin_exists()

    assert admin_users.count(db) == 0
    assert "manage.py create-admin" in caplog.text


def test_interrupt_does_not_kill_the_server(db, console, monkeypatch, caplog):
    """Ctrl+C на вопросе о пароле — отказ от создания, а не аварийная остановка."""
    monkeypatch.setattr(bootstrap.sys, "stdin", FakeTTY())
    monkeypatch.setattr(bootstrap, "input", lambda _prompt: "operator", raising=False)
    monkeypatch.setattr(bootstrap.getpass, "getpass", _raise(KeyboardInterrupt()))

    bootstrap.ensure_admin_exists()  # не должно пробросить исключение

    assert admin_users.count(db) == 0
    assert "прервано" in caplog.text


def _forbidden(reason: str):
    def _fail(*_args, **_kwargs):
        raise AssertionError(reason)

    return _fail


def _raise(exc: BaseException):
    def _fail(*_args, **_kwargs):
        raise exc

    return _fail
