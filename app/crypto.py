"""Шифрование содержимого писем: тексты в БД и файлы вложений на диске.

Схема — Fernet (AES-128-CBC + HMAC) с ключом из файла `MG_ENCRYPTION_KEY_FILE`.
Ключ не задан — данные пишутся открытым текстом, как раньше; о таком состоянии
предупреждает `Settings.insecure_defaults()`. Уже записанное читается в обоих
случаях: зашифрованный текст в БД отличается префиксом, зашифрованный файл —
магической сигнатурой, всё прочее отдаётся как есть. Поэтому включение ключа
не требует перешифровки старых данных, а его отсутствие не прячет их.

Шифруется только содержимое (тела, диагностика отказов, байты вложений).
Адреса и тема — намеренно нет: по ним работают поиск и фильтры панели/API
(`ilike` в services/messages.py), шифрование сделало бы их бесполезными.

Файл вложения — последовательность независимо зашифрованных кусков:
`MGENC1\n`, затем повторяющееся «4 байта длины (BE) + токен Fernet». Кусками,
а не одним токеном, потому что загрузки пишутся потоком (uploads.py) и файл
целиком в памяти не бывает.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .constants import UPLOAD_CHUNK_BYTES

logger = logging.getLogger(__name__)

#: Префикс зашифрованного значения в текстовой колонке БД.
TEXT_PREFIX = "enc:"
#: Сигнатура зашифрованного файла вложения.
FILE_MAGIC = b"MGENC1\n"

_cache: tuple[str, Fernet] | None = None


class DecryptionError(RuntimeError):
    """Данные зашифрованы, но расшифровать их нечем или не получилось."""


def fernet() -> Fernet | None:
    """Ключ из `MG_ENCRYPTION_KEY_FILE`, загруженный один раз. Нет пути — None.

    Смена ключа на живом сервисе бессмысленна (старые данные стали бы
    нечитаемыми), поэтому перечитывания по mtime, как у ключей DKIM, здесь нет.
    """
    global _cache
    path = settings.encryption_key_path
    if path is None:
        return None
    key = str(path)
    if _cache is None or _cache[0] != key:
        try:
            _cache = (key, Fernet(path.read_bytes().strip()))
        except FileNotFoundError as exc:
            raise DecryptionError(
                f"Файл ключа шифрования не найден: {path}. "
                "Создайте его: python manage.py encryption-keygen"
            ) from exc
        except ValueError as exc:
            raise DecryptionError(f"Файл {path} не содержит корректный ключ Fernet") from exc
    return _cache[1]


def _require_fernet() -> Fernet:
    f = fernet()
    if f is None:
        raise DecryptionError(
            "Данные зашифрованы, а MG_ENCRYPTION_KEY_FILE не задан — "
            "без исходного ключа они невосстановимы"
        )
    return f


# --- Текст в БД -------------------------------------------------------------------- #


def encrypt_text(value: str) -> str:
    f = fernet()
    if f is None:
        return value
    return TEXT_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str) -> str:
    """Расшифровывает значение из БД; незашифрованное возвращает как есть."""
    if not value.startswith(TEXT_PREFIX):
        return value
    token = value[len(TEXT_PREFIX):].encode("ascii")
    try:
        return _require_fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError(
            "Не удалось расшифровать данные: ключ в MG_ENCRYPTION_KEY_FILE "
            "не тот, которым они были зашифрованы"
        ) from exc


# --- Файлы вложений ---------------------------------------------------------------- #


class _EncryptedSink:
    """Пишущая обёртка: каждый `write()` становится отдельным токеном Fernet."""

    def __init__(self, fh, f: Fernet):
        self._fh = fh
        self._f = f
        fh.write(FILE_MAGIC)

    def write(self, chunk: bytes) -> int:
        token = self._f.encrypt(bytes(chunk))
        self._fh.write(len(token).to_bytes(4, "big"))
        self._fh.write(token)
        return len(chunk)


@contextmanager
def open_sink(path: Path):
    """Открывает файл на запись: с ключом — шифрующий, без — обычный."""
    f = fernet()
    with path.open("wb") as fh:
        yield fh if f is None else _EncryptedSink(fh, f)


def write_file(path: Path, data: bytes) -> None:
    with open_sink(path) as sink:
        for start in range(0, len(data), UPLOAD_CHUNK_BYTES):
            sink.write(data[start:start + UPLOAD_CHUNK_BYTES])


def is_encrypted_file(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(FILE_MAGIC)) == FILE_MAGIC


def iter_file(path: Path) -> Iterator[bytes]:
    """Содержимое файла кусками открытого текста; незашифрованный — как есть."""
    with path.open("rb") as fh:
        head = fh.read(len(FILE_MAGIC))
        if head != FILE_MAGIC:
            while head:
                yield head
                head = fh.read(UPLOAD_CHUNK_BYTES)
            return
        f = _require_fernet()
        while size_raw := fh.read(4):
            if len(size_raw) < 4:
                raise DecryptionError(f"Файл вложения обрезан: {path}")
            token = fh.read(int.from_bytes(size_raw, "big"))
            try:
                yield f.decrypt(token)
            except InvalidToken as exc:
                raise DecryptionError(
                    f"Не удалось расшифровать вложение {path}: ключ не тот "
                    "или файл повреждён"
                ) from exc


def read_file(path: Path) -> bytes:
    return b"".join(iter_file(path))
