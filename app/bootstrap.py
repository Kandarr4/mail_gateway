"""Первоначальная настройка при запуске процесса.

Шаги, которые выполняются один раз до старта сервера и требуют живого
терминала. Всё, что здесь есть, обязано корректно вести себя и без него:
служба Windows, контейнер и `pytest` работают без stdin.
"""

import getpass
import logging
import sys

from sqlalchemy import select

from .api.errors import ApiError
from .config import settings
from .db import session_scope
from .services import admin_users, api_keys, domains

logger = logging.getLogger(__name__)

DEFAULT_USERNAME = "admin"
#: Сколько раз переспрашивать, прежде чем сдаться и продолжить запуск.
MAX_ATTEMPTS = 3

DEFAULT_CLIENT_NAME = "Default"


def ensure_default_client_from_env() -> None:
    """Дев/тестовый бутстрап: `MG_API_TOKENS` заводит клиента и его ключи в БД.

    В бою клиенты, домены и ключи создаются через веб-панель; этот шаг
    существует ради локальной разработки и обратной совместимости — токен из
    окружения продолжает работать, потому что на старте идемпотентно
    превращается в запись `ApiKey` клиента «Default» с доменом `MG_DOMAIN`.
    Повторный запуск ничего не дублирует: и клиент, и домен, и каждый ключ
    сначала ищутся, потом создаются.
    """
    from .models import Client

    with session_scope() as db:
        client = db.scalars(select(Client).where(Client.name == DEFAULT_CLIENT_NAME)).first()
        if client is None:
            client = Client(name=DEFAULT_CLIENT_NAME)
            db.add(client)
            db.flush()
            logger.info("Создан клиент по умолчанию для токенов из MG_API_TOKENS")

        if domains.by_name(db, settings.domain) is None:
            domain = domains.create(db, client.id, settings.domain)
            # Домен инстанса наследует ключ DKIM из настроек — поведение
            # одно-доменной установки не меняется.
            domain.dkim_private_key_file = settings.dkim_private_key_file or None
            domain.dkim_selector = settings.dkim_selector

        for raw in settings.tokens:
            if api_keys.by_hash(db, raw) is None:
                api_keys.create_with_raw(db, client.id, "env-bootstrap", raw)


def ensure_admin_exists() -> None:
    """Заводит первую учётную запись панели, если в базе нет ни одной.

    Пустая таблица учётных записей означает, что панель поднята, доступна по
    сети и войти в неё не может никто — включая владельца. Положение тупиковое:
    завести пользователя можно только отдельной командой, о которой надо знать.

    Приём почты от этого не зависит, поэтому отсутствие терминала — не повод
    падать: в неинтерактивном запуске просто громко пишем в журнал, что
    делать.
    """
    if not settings.web_enabled:
        return

    with session_scope() as db:
        if admin_users.count(db) > 0:
            return

    if not _interactive():
        logger.error(
            "В базе нет ни одной учётной записи панели, а терминала нет — "
            "войти в /admin будет некому. Создайте её командой "
            "`python manage.py create-admin admin` (в сборке: "
            "`MailGateway.exe create-admin admin`) либо запустите значок в "
            "трее — мастер первого запуска предложит создать её сам"
        )
        return

    print("\n=== Первый запуск: в базе нет ни одной учётной записи панели ===")
    print("Создайте администратора для входа в /admin.\n")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if _create_from_console():
                return
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C на вопросе о пароле — это отказ создавать учётную запись,
            # а не аварийная остановка почтового сервера.
            print()
            logger.warning(
                "Создание учётной записи прервано. Панель останется недоступной; "
                "создать позже: python manage.py create-admin admin"
            )
            return
        if attempt < MAX_ATTEMPTS:
            print()

    logger.warning(
        "Учётная запись так и не создана. Панель останется недоступной; "
        "создать позже: python manage.py create-admin admin"
    )


def _create_from_console() -> bool:
    """Один заход диалога. True — учётная запись создана."""
    username = input(f"Имя пользователя [{DEFAULT_USERNAME}]: ").strip() or DEFAULT_USERNAME

    # Пароль читаем без эха и никогда не принимаем аргументом команды:
    # аргументы видны в списке процессов и оседают в истории оболочки.
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.")
        return False

    try:
        with session_scope() as db:
            user = admin_users.create(db, username, password)
            name = user.username
    except ApiError as exc:
        print(f"Ошибка: {exc.detail}")
        return False

    print(f"Создана учётная запись: {name}\n")
    return True


def _interactive() -> bool:
    """Есть ли живой терминал, у которого вообще можно что-то спросить.

    Под службой Windows stdin отсутствует или закрыт, и `input()` там не
    заблокируется, а немедленно бросит EOFError на каждой итерации.
    """
    stream = sys.stdin
    if stream is None or stream.closed:
        return False
    try:
        return stream.isatty()
    except (ValueError, OSError):
        return False
