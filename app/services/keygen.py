"""Генерация секретов и первичного `.env` — без побочного вывода.

Ядро, которым пользуются и `manage.py` (печатает результат в консоль), и мастер
первого запуска (`tray/setup_dialog.py`, показывает результат в GUI). Здесь
только файловые операции и возврат значений; печать и диалоги — на стороне
вызывающего.

Существующие файлы не перезаписываются: `generate_*` бросают `FileExistsError`,
а `bootstrap_env` при уже созданном ключе переиспользует его, но никогда не
трогает уже существующий `.env`.
"""

import base64
import contextlib
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Well-known SID службы LocalSystem: под ней по умолчанию ставится служба
#: (`service.py` → `win32serviceutil.HandleCommandLine`). SID вместо имени
#: "SYSTEM" — на локализованной Windows имя учётной записи другое.
_LOCAL_SYSTEM_SID = "*S-1-5-18"


def generate_api_token() -> str:
    """Случайный токен для `MG_API_TOKENS`.

    32 байта энтропии — столько же, сколько у ключа Fernet; перебор
    бессмысленен. При старте сервера превращается в ключ клиента «Default»
    (`ensure_default_client_from_env`); в БД ляжет только его SHA-256.
    """
    return secrets.token_urlsafe(32)


def restrict_to_owner(path: Path) -> None:
    """Закрывает доступ к секрету всем, кроме владельца машины и службы.

    Файл (ключ подписи, ключ шифрования, `.env` с токеном) не должен читаться
    остальными учётными записями. Но служба Mail Gateway по умолчанию работает
    под LocalSystem и обязана эти файлы читать — иначе подпись DKIM и
    расшифровка содержимого молча ломаются. Поэтому доступ получают ровно
    интерактивный пользователь и SYSTEM.
    """
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/inheritance:r"], check=False,
                       capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", f"{_LOCAL_SYSTEM_SID}:R"],
                       check=False, capture_output=True)
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", str(path), "/grant:r", f"{user}:R"], check=False,
                           capture_output=True)
    else:
        path.chmod(0o600)


def generate_encryption_key(path: Path, *, force: bool = False) -> Path:
    """Создаёт ключ Fernet по пути `path`. Без `force` не перезаписывает.

    Перезапись существующего ключа делает уже зашифрованные письма и резервные
    копии невосстановимыми — только осознанно.
    """
    from cryptography.fernet import Fernet

    if path.exists() and not force:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet.generate_key())
    restrict_to_owner(path)
    return path


def generate_dkim_key(path: Path, *, bits: int = 2048, force: bool = False) -> str:
    """Создаёт закрытый ключ DKIM (PKCS#8 PEM), возвращает base64 открытого.

    Возвращённое значение подставляется в TXT-запись DNS
    (`dkim_signer.dns_record`). Без `force` существующий ключ не трогается:
    его перезапись мгновенно ломает подпись всех писем, пока в DNS стоит
    старая запись.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if path.exists() and not force:
        raise FileExistsError(path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    restrict_to_owner(path)
    return _public_base64(key)


def dkim_public_base64(path: Path) -> str:
    """base64 открытого ключа из уже существующего закрытого — чтобы заново
    показать оператору TXT-запись, не создавая новый ключ."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return _public_base64(load_pem_private_key(path.read_bytes(), password=None))


def _public_base64(private_key) -> str:
    from cryptography.hazmat.primitives import serialization

    der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode()


@dataclass
class BootstrapResult:
    """Итог первичной генерации: пути к созданным файлам и запись для DNS."""

    env_path: Path
    encryption_key_path: Path
    dkim_key_path: Path
    api_token: str
    dkim_selector: str
    dns_name: str
    dns_value: str


def bootstrap_env(
    base_dir: Path,
    *,
    domain: str,
    api_port: int,
    dkim_selector: str = "mail",
    dkim_bits: int = 2048,
) -> BootstrapResult:
    """Пишет полный рабочий `.env` из шаблона `.env.example` и генерирует секреты.

    Вызывать только при полном отсутствии `.env`: существующий файл не трогаем
    (частично заполненный «дочинивать» молча нельзя). Генерирует токен API,
    ключ Fernet и пару DKIM, проставляет их в `.env` вместе с доменом, портом и
    селектором, ограничивает доступ к `.env` и ключам и возвращает запись,
    которую оператор ставит в DNS.

    Ключи, если уже лежат на месте, переиспользуются (для DKIM — с чтением
    открытого ключа из существующего закрытого), но не перезаписываются.
    """
    from .dkim_signer import dns_record

    domain = domain.strip().lower()
    env_path = base_dir / ".env"
    if env_path.exists():
        raise FileExistsError(env_path)
    template = base_dir / ".env.example"
    if not template.is_file():
        raise FileNotFoundError(template)

    encryption_rel = "data/keys/encryption.key"
    dkim_rel = f"data/dkim/{domain}.private"
    encryption_path = base_dir / encryption_rel
    dkim_path = base_dir / dkim_rel

    # Ключ уже есть — переиспользуем, не перезаписываем.
    with contextlib.suppress(FileExistsError):
        generate_encryption_key(encryption_path)

    try:
        public_b64 = generate_dkim_key(dkim_path, bits=dkim_bits)
    except FileExistsError:
        public_b64 = dkim_public_base64(dkim_path)

    api_token = generate_api_token()
    dns_name, dns_value = dns_record(domain, dkim_selector, public_b64)

    text = template.read_text(encoding="utf-8")
    text = _set_env(text, "MG_DOMAIN", domain)
    text = _set_env(text, "MG_API_PORT", str(api_port))
    text = _set_env(text, "MG_API_TOKENS", api_token)
    text = _set_env(text, "MG_ENCRYPTION_KEY_FILE", encryption_rel)
    text = _set_env(text, "MG_DKIM_PRIVATE_KEY_FILE", dkim_rel)
    text = _set_env(text, "MG_DKIM_SELECTOR", dkim_selector)

    env_path.write_text(text, encoding="utf-8")
    restrict_to_owner(env_path)

    return BootstrapResult(
        env_path=env_path,
        encryption_key_path=encryption_path,
        dkim_key_path=dkim_path,
        api_token=api_token,
        dkim_selector=dkim_selector,
        dns_name=dns_name,
        dns_value=dns_value,
    )


def _set_env(text: str, key: str, value: str) -> str:
    """Заменяет значение переменной в тексте `.env`, сохраняя комментарии.

    Замену делает функцией, а не строкой-шаблоном: значение содержит пути с
    обратными слэшами и base64 — их спецсимволы не должны толковаться как
    группы подстановки регулярного выражения.
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new_text, count = pattern.subn(lambda _m: f"{key}={value}", text, count=1)
    if count == 0:
        new_text = text.rstrip("\n") + f"\n{key}={value}\n"
    return new_text
