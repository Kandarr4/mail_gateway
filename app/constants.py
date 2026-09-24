"""Единственный источник правды для доменных констант и словарей статусов.

Магические строки ("queued", "incoming", ...) не должны встречаться в коде —
только эти перечисления.
"""

from enum import StrEnum
from typing import Final


class Direction(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class MessageStatus(StrEnum):
    """Входящее письмо сразу RECEIVED; исходящее: QUEUED → SENDING → SENT|FAILED.

    BOUNCED — отдельный исход, наступающий уже после SENT: принимающий сервер
    подтвердил приём, а затем прислал отчёт о невозможности доставить. Смешивать
    его с FAILED нельзя: FAILED означает «мы не смогли отправить», BOUNCED —
    «отправили, но адресат не существует», и действия по ним разные.
    """

    RECEIVED = "received"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"


class OutboundMode(StrEnum):
    DIRECT = "direct"
    RELAY = "relay"


class InboundAuthPolicy(StrEnum):
    """Что делать с письмом, не прошедшим проверку подлинности отправителя.

    OFF        — не проверять вовсе (проверки стоят DNS-запросов);
    ANNOTATE   — проверять и записывать результат, принимать всё;
    ENFORCE    — отклонять то, что домен отправителя сам просит отклонять (DMARC p=reject).

    По умолчанию ANNOTATE: включать отказы сразу опасно — сначала надо
    посмотреть в журнале, что именно начало бы отбиваться.
    """

    OFF = "off"
    ANNOTATE = "annotate"
    ENFORCE = "enforce"


class AuthResult(StrEnum):
    """Значения из RFC 8601 (Authentication-Results)."""

    PASS = "pass"  # noqa: S105 — вердикт SPF/DKIM, а не пароль
    FAIL = "fail"
    SOFTFAIL = "softfail"
    NEUTRAL = "neutral"
    NONE = "none"
    TEMPERROR = "temperror"
    PERMERROR = "permerror"


# --- Лимиты и таймауты (не разбрасывать по коду) ---------------------------- #

SMTP_TIMEOUT_SECONDS: Final = 30
SMTP_PORT_MX: Final = 25

WORKER_IDLE_SECONDS: Final = 5.0
UPLOAD_CHUNK_BYTES: Final = 64 * 1024

#: Сколько ждать заголовок PROXY protocol, если он включён.
PROXY_PROTOCOL_TIMEOUT: Final = 3.0
#: Потолок числа SMTP-команд на соединение — против забивания мусорными командами.
SMTP_COMMAND_LIMIT: Final = 100

#: Имена, недопустимые как имена файлов в Windows (даже с расширением).
WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
