"""Хеширование паролей. Единственное место, где пароль превращается в хеш.

Используется `scrypt` из стандартной библиотеки: это функция выведения ключа с
настраиваемой стоимостью, устойчивая к перебору на видеокартах. Компилируемых
зависимостей вроде bcrypt не требуется — важно, потому что колёс под свежий
Python может не оказаться.
"""

import hashlib
import hmac
import secrets

#: Параметры стоимости. N=2^14 — около 50 мс и 16 МиБ памяти на проверку:
#: незаметно человеку и дорого перебору. Выше поднимать нельзя без оглядки на
#: память: scrypt требует 128 * N * r байт на каждую параллельную проверку, и
#: при N=2^15 это уже 32 МиБ на запрос.
_N = 2**14
_R = 8
_P = 1
_DK_LEN = 64
_SALT_BYTES = 16

#: Явный потолок памяти. По умолчанию OpenSSL разрешает 32 МиБ и отказывает
#: с невнятным "memory limit exceeded", если параметрам нужно ровно столько же.
_MAX_MEM = 64 * 1024 * 1024

_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Возвращает строку вида `scrypt$N$r$p$соль$хеш` — самодостаточную для проверки."""
    if not password:
        raise ValueError("Пароль не может быть пустым")

    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P)
    return f"{_PREFIX}${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Проверяет пароль. Никогда не бросает исключений и не различает по времени
    «нет такого пользователя» и «неверный пароль»."""
    if not stored:
        # Пользователя нет. Всё равно считаем хеш, чтобы по времени ответа
        # нельзя было выяснить, какие имена существуют.
        _derive(password, b"\x00" * _SALT_BYTES, _N, _R, _P)
        return False

    try:
        prefix, n, r, p, salt_hex, digest_hex = stored.split("$")
        if prefix != _PREFIX:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = _derive(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """True, если хеш посчитан с устаревшими параметрами стоимости."""
    try:
        prefix, n, r, p, _salt, _digest = stored.split("$")
    except ValueError:
        return True
    return prefix != _PREFIX or (int(n), int(r), int(p)) != (_N, _R, _P)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DK_LEN, maxmem=_MAX_MEM
    )
