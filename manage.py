"""Служебные команды шлюза.

    python manage.py create-admin <имя>      создать учётную запись панели
    python manage.py set-password <имя>      сменить пароль
    python manage.py list-admins             список учётных записей
    python manage.py dkim-keygen             создать ключ DKIM и запись для DNS
    python manage.py encryption-keygen       создать ключ шифрования содержимого

Пароль запрашивается интерактивно и не отображается при вводе. Передавать его
аргументом командной строки нельзя: аргументы видны в списке процессов и
оседают в истории команд.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.api.errors import ApiError
from app.db import session_scope
from app.schema_setup import prepare_database
from app.services import admin_users


def ask_password(prompt: str = "Пароль: ") -> str:
    password = getpass.getpass(prompt)
    if password != getpass.getpass("Повторите: "):
        raise SystemExit("Пароли не совпадают")
    return password


def create_admin(username: str, password: str | None) -> int:
    with session_scope() as db:
        user = admin_users.create(db, username, password or ask_password())
        print(f"Создана учётная запись: {user.username}")
    return 0


def set_password(username: str, password: str | None) -> int:
    with session_scope() as db:
        user = admin_users.set_password(db, username, password or ask_password("Новый пароль: "))
        print(f"Пароль изменён: {user.username}")
    return 0


def dkim_keygen(bits: int, force: bool, domain: str | None) -> int:
    """Создаёт пару ключей DKIM и печатает запись для DNS.

    Без `--domain` работает по-старому: ключ по пути из
    `MG_DKIM_PRIVATE_KEY_FILE` для домена инстанса. С `--domain` — ключ для
    отдельного домена клиента в `data/dkim/<домен>.private`; путь затем
    указывается у домена в панели (/admin/clients).
    """
    from app.config import BASE_DIR, settings
    from app.services import dkim_signer, keygen

    if domain:
        domain = domain.strip().lower()
        path = BASE_DIR / "data" / "dkim" / f"{domain}.private"
        record_domain = domain
    else:
        path = settings.dkim_key_path
        record_domain = settings.domain
        if path is None:
            print(
                "Не задан MG_DKIM_PRIVATE_KEY_FILE — некуда сохранять ключ.\n"
                "Добавьте в .env, например: MG_DKIM_PRIVATE_KEY_FILE=data/dkim/mail.private\n"
                "Либо создайте ключ для конкретного домена: --domain example.kz",
                file=sys.stderr,
            )
            return 1

    try:
        public_b64 = keygen.generate_dkim_key(path, bits=bits, force=force)
    except FileExistsError:
        # Перезапись ключа мгновенно ломает подпись всех писем, пока в DNS
        # стоит старая запись. Только по явному требованию.
        print(f"Ключ уже существует: {path}\nПерезаписать: --force", file=sys.stderr)
        return 1

    name, value = dkim_signer.dns_record(record_domain, settings.dkim_selector, public_b64)

    print(f"Закрытый ключ сохранён: {path}")
    print("\nДобавьте в DNS запись TXT:\n")
    print(f"  имя:      {name}")
    print(f"  значение: {value}\n")
    print("Подпись заработает после того, как запись разойдётся по DNS.")
    print("Проверить: nslookup -type=TXT " + name)
    # DKIM — лишь одна из шести записей, и без остальных (MX, SPF, DMARC, PTR)
    # почта либо не придёт, либо уйдёт в спам. Показываем, где взять весь набор.
    print(f"\nОстальные нужные записи (MX, SPF, DMARC, PTR): dns-records --domain {record_domain}")
    return 0


def encryption_keygen(force: bool) -> int:
    """Создаёт ключ Fernet для шифрования тел писем и вложений.

    Путь берётся из `MG_ENCRYPTION_KEY_FILE`. Перезапись существующего ключа —
    только по `--force`: без исходного ключа уже зашифрованные письма и
    резервные копии невосстановимы.
    """
    from app.config import settings
    from app.services import keygen

    path = settings.encryption_key_path
    if path is None:
        print(
            "Не задан MG_ENCRYPTION_KEY_FILE — некуда сохранять ключ.\n"
            "Добавьте в .env, например: MG_ENCRYPTION_KEY_FILE=data/keys/encryption.key",
            file=sys.stderr,
        )
        return 1

    try:
        keygen.generate_encryption_key(path, force=force)
    except FileExistsError:
        print(
            f"Ключ уже существует: {path}\n"
            "Перезапись сделает уже зашифрованные данные нечитаемыми. "
            "Только осознанно: --force",
            file=sys.stderr,
        )
        return 1

    print(f"Ключ шифрования сохранён: {path}")
    print(
        "\nШифруются тела писем в БД и файлы вложений — с момента включения,\n"
        "записанное ранее остаётся открытым. Храните копию ключа отдельно от\n"
        "резервных копий БД: без ключа их содержимое невосстановимо."
    )
    return 0


def dns_records(domain: str | None, check: bool) -> int:
    """Печатает все записи DNS, нужные домену, и (по `--check`) их состояние."""
    from app.config import BASE_DIR, settings
    from app.services import dns_setup

    name = (domain or settings.domain).strip().lower()
    checked = dns_setup.check(dns_setup.required_records(name)) if check else None
    text = dns_setup.as_text(name, checked)
    print()
    print(text)

    # Собранный exe пишет в консоль, минуя перенаправление вывода (он GUI-шный
    # и подключается к консоли сам), так что `> файл` тут не работает. А
    # копировать оператору нужно длинные значения ключей — кладём файл рядом.
    saved = BASE_DIR / "dns_record.txt"
    try:
        saved.write_text(text, encoding="utf-8")
        print(f"Сохранено в файл: {saved}")
    except OSError as exc:
        print(f"Не удалось сохранить {saved}: {exc}", file=sys.stderr)
    return 0


def list_admins() -> int:
    from sqlalchemy import select

    from app.models import AdminUser

    with session_scope() as db:
        users = db.scalars(select(AdminUser).order_by(AdminUser.username)).all()
        if not users:
            print("Учётных записей нет. Создайте: python manage.py create-admin admin")
            return 0
        print(f"{'ИМЯ':20} {'СОСТОЯНИЕ':12} ПОСЛЕДНИЙ ВХОД")
        for user in users:
            state = "активна" if user.is_active else "отключена"
            last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "никогда"
            print(f"{user.username:20} {state:12} {last}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`argv` — для вызова из единой точки входа (mailgateway.py); None — CLI."""
    parser = argparse.ArgumentParser(description="Служебные команды Mail Gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="создать учётную запись панели")
    create.add_argument("username")
    create.add_argument("--password", help="не рекомендуется: попадёт в историю команд")

    change = sub.add_parser("set-password", help="сменить пароль")
    change.add_argument("username")
    change.add_argument("--password", help="не рекомендуется: попадёт в историю команд")

    sub.add_parser("list-admins", help="список учётных записей")

    keygen = sub.add_parser("dkim-keygen", help="создать ключ DKIM и показать запись для DNS")
    keygen.add_argument("--bits", type=int, default=2048, choices=(1024, 2048, 4096))
    keygen.add_argument("--force", action="store_true", help="перезаписать существующий ключ")
    keygen.add_argument("--domain", help="домен клиента: ключ в data/dkim/<домен>.private")

    enc = sub.add_parser(
        "encryption-keygen", help="создать ключ шифрования тел писем и вложений"
    )
    enc.add_argument("--force", action="store_true", help="перезаписать существующий ключ")

    dns = sub.add_parser("dns-records", help="какие записи DNS нужны домену")
    dns.add_argument("--domain", help="домен клиента; по умолчанию — домен инстанса")
    dns.add_argument("--check", action="store_true", help="сверить с тем, что уже в DNS")

    args = parser.parse_args(argv)

    # Генерация ключей и справка по DNS с базой не работают и нужны ещё до её
    # создания.
    if args.command == "dkim-keygen":
        return dkim_keygen(args.bits, args.force, args.domain)
    if args.command == "encryption-keygen":
        return encryption_keygen(args.force)
    if args.command == "dns-records":
        return dns_records(args.domain, args.check)

    prepare_database()

    try:
        if args.command == "create-admin":
            return create_admin(args.username, args.password)
        if args.command == "set-password":
            return set_password(args.username, args.password)
        return list_admins()
    except ApiError as exc:
        print(f"Ошибка: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
