"""Лицензия Mail Gateway: проверка файла `license.lic` от сервера Somnium.

Формат общий для всех продуктов Somnium (см. somnium_licensing):
`base64(JSON + RSA-PSS-подпись SHA-256, 256 байт)`. Здесь встроен публичный
ключ сервера лицензирования; подделать файл без закрытого ключа нельзя.

Лицензия обязательна для *почты*, а не для запуска. Служба поднимается
всегда: панель управления, API просмотра и настройки работают — иначе
установить лицензию было бы некуда, а оператор видел бы остановленную службу
и требование прав администратора. Без действующего файла шлюз просто не
обрабатывает почту:

* приём по SMTP отвечает временным отказом (`4xx`) — отправитель повторит
  доставку, и письма дойдут после установки лицензии, а не потеряются;
* исходящая очередь встаёт: письма ждут в базе, ничего не теряется;
* постановка письма в очередь через API отвечает `503`.

Разрешение проверяется на каждом письме (`mail_allowed`), а не один раз на
старте: положенная в работающий шлюз лицензия включает почту сама, без
перезапуска службы, и так же сама выключает её по окончании срока.

Действующая лицензия ограничивает число зарегистрированных доменов
(`max_domains`) — см. `check_domain_limit`.
"""

import base64
import binascii
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import BASE_DIR

logger = logging.getLogger(__name__)

PROGRAM_ID = "mail-gateway"
LICENSE_FILE = BASE_DIR / "license.lic"
LICENSING_URL = "https://somnium.kz/licensing"

#: Длина подписи RSA-2048 в байтах — столько отрезается с конца файла.
_SIGNATURE_LEN = 256

#: Публичный ключ сервера лицензирования Somnium. Подменить его можно только в
#: коде: тесты присваивают `_PUBLIC_KEY_PEM` своего ключа. Переменной окружения
#: для этого нет намеренно — через неё лицензию можно было бы выписать себе
#: самому, а исходники проекта открыты.
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAoThntrmotEYtNtaRBz4o
ZhXXVjakqK49HyGN5Gcf/GDgWv9G7Pb2xNu28Mzz551DKqfdJ02YS3WaINv57FBV
/4Mg8BXH5q/JvIBJU+2difot8VDmIME0gdtt2mmsJcwrF/uNdPy2nW37o/rc/rEG
DeycTPk5SE7oRaB2XFXyh+WvfpGQWlwaWAh8te0XDiNE5veGv5nAGr3cACV3KNBr
o86bu+HoZ2IorvC4XabZuW82Ap9S2YcHcFqN1RKIqaUhXwdraq5ehsb76jaglNEk
wecg8aL12nQsEPwhYdzl5DjQxidjiTIduU6MDywkxbokwVdnBj4Jic5F0/nfAuVM
dQIDAQAB
-----END PUBLIC KEY-----"""

REQUIRED_FIELDS = ("company_name", "issue_date", "expiry_date", "issued_by", "max_domains")

#: За сколько дней до конца срока в журнале появляется предупреждение.
EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class LicenseStatus:
    """Итог проверки — всё, что нужно панели, трею и ограничителю доменов."""

    valid: bool
    reason: str
    data: dict | None = None

    @property
    def company(self) -> str:
        return (self.data or {}).get("company_name", "—")

    @property
    def max_domains(self) -> int | None:
        if not self.valid or self.data is None:
            return None
        try:
            return int(self.data["max_domains"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def expiry_date(self) -> datetime | None:
        if self.data is None:
            return None
        try:
            return datetime.fromisoformat(self.data["expiry_date"])
        except (KeyError, ValueError, TypeError):
            return None

    @property
    def days_remaining(self) -> int | None:
        expiry = self.expiry_date
        if expiry is None:
            return None
        return (expiry - datetime.now()).days


def _public_key():
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_public_key(_PUBLIC_KEY_PEM)


def verify_file(path: Path) -> LicenseStatus:
    """Полная проверка файла лицензии: подпись, поля, срок."""
    if not path.is_file():
        return LicenseStatus(False, "Файл лицензии не найден")

    try:
        decoded = base64.b64decode(path.read_bytes(), validate=True)
    except (binascii.Error, ValueError, OSError):
        return LicenseStatus(False, "Файл лицензии повреждён: не читается")
    if len(decoded) <= _SIGNATURE_LEN:
        return LicenseStatus(False, "Файл лицензии повреждён: неполный")

    payload, signature = decoded[:-_SIGNATURE_LEN], decoded[-_SIGNATURE_LEN:]

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        _public_key().verify(
            signature,
            payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return LicenseStatus(False, "Подпись лицензии недействительна")

    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return LicenseStatus(False, "Данные лицензии не разбираются")

    if data.get("program_id") != PROGRAM_ID:
        return LicenseStatus(False, f"Лицензия выдана для другой программы: {data.get('program_id')}", data)

    for field in REQUIRED_FIELDS:
        if field not in data:
            return LicenseStatus(False, f"В лицензии нет поля {field}", data)

    try:
        issue = datetime.fromisoformat(data["issue_date"])
        expiry = datetime.fromisoformat(data["expiry_date"])
    except ValueError:
        return LicenseStatus(False, "Даты лицензии не разбираются", data)

    now = datetime.now()
    if now < issue:
        # Часы, переведённые назад, не должны «продлевать» лицензию.
        return LicenseStatus(False, "Системное время раньше даты выдачи лицензии", data)
    if issue > expiry:
        return LicenseStatus(False, "Дата выдачи позже даты окончания — файл повреждён", data)
    if now > expiry:
        days = (now - expiry).days
        return LicenseStatus(False, f"Срок лицензии истёк {days} дн. назад", data)

    return LicenseStatus(True, "Действительна", data)


# --- Кеш: панель и трей опрашивают статус часто, файл меняется редко ----------- #

_CACHE_TTL_SECONDS = 60
_cache: dict = {"status": None, "at": 0.0, "mtime": None}


def status(*, use_cache: bool = True) -> LicenseStatus:
    """Текущий статус лицензии инстанса (файл `license.lic` в корне)."""
    mtime = LICENSE_FILE.stat().st_mtime if LICENSE_FILE.is_file() else None
    fresh = (
        use_cache
        and _cache["status"] is not None
        and _cache["mtime"] == mtime
        and time.monotonic() - _cache["at"] < _CACHE_TTL_SECONDS
    )
    if not fresh:
        _cache["status"] = verify_file(LICENSE_FILE)
        _cache["at"] = time.monotonic()
        _cache["mtime"] = mtime
    return _cache["status"]


def reset_cache() -> None:
    _cache.update(status=None, at=0.0, mtime=None)


def install_license_file(source: Path) -> LicenseStatus:
    """Единственный путь установки нового файла — для панели и трея.

    Сначала полная проверка, затем копирование на место и сброс кеша. Битый
    файл не затирает работающий.
    """
    result = verify_file(source)
    if not result.valid:
        return result
    shutil.copy2(source, LICENSE_FILE)
    reset_cache()
    logger.info("Установлена лицензия: %s, действует до %s", result.company, result.data["expiry_date"])
    return result


def check_domain_limit(db) -> tuple[bool, str]:
    """Лимит числа доменов действующей лицензии.

    Сюда попадают только процессы, прошедшие `require_valid` на старте, то
    есть лицензия заведомо действительна. `limit is None` остаётся возможным
    для лицензии без поля `max_domains` — такой файл доменов не ограничивает.
    """
    from . import domains

    current = status()
    limit = current.max_domains
    if limit is None:
        return True, ""

    used = domains.count_all(db)
    if used >= limit:
        return False, (
            f"Достигнут лимит лицензии: {limit} домен(ов). "
            f"Для расширения обратитесь за новой лицензией: {LICENSING_URL}"
        )
    return True, ""


# --- Принуждение: без действующей лицензии почта не обрабатывается -------------- #


def mail_allowed() -> bool:
    """Можно ли принимать и отправлять почту прямо сейчас.

    Спрашивается на каждом письме, а не однократно на старте: положенная в
    работающий шлюз лицензия включает почту сама, без перезапуска службы. Это
    дёшево — файл перечитывается только при смене mtime и не чаще раза в
    минуту (см. `status`).
    """
    return status().valid


def suspension_notice() -> str:
    """Почему почта не обрабатывается и что делать — для журнала и панели.

    Один текст на все каналы, чтобы человек, увидевший его в журнале, в
    панели или в трее, прочитал одинаковые указания.
    """
    return (
        f"Обработка почты приостановлена: {status().reason}. "
        f"Приём и отправка возобновятся сразу после установки действующей "
        f"лицензии ({LICENSE_FILE}), перезапуск не нужен. "
        f"Получить или продлить: {LICENSING_URL}"
    )


def log_status() -> None:
    """Состояние лицензии в журнал на старте — и напоминание о продлении."""
    current = status(use_cache=False)
    if not current.valid:
        logger.warning("ЛИЦЕНЗИЯ: %s", suspension_notice())
        return

    logger.info(
        "Лицензия: %s, до %s (осталось %s дн.)",
        current.company,
        current.data["expiry_date"][:10],
        current.days_remaining,
    )
    days = current.days_remaining
    if days is not None and days <= EXPIRY_WARNING_DAYS:
        logger.warning(
            "ЛИЦЕНЗИЯ: истекает через %s дн. — после этого шлюз перестанет "
            "принимать и отправлять почту, продлите заранее: %s",
            days, LICENSING_URL,
        )
