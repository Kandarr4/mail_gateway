"""Проверка подлинности отправителя входящего письма: SPF, DKIM, DMARC.

Без этих проверок шлюз принимает и показывает как настоящее любое письмо,
в котором в поле `From` написано что угодно — подделка адреса руководителя
не требует ничего, кроме telnet.

Проверки стоят DNS-запросов и выполняются синхронно, поэтому вызываются из
рабочего потока, а не из цикла событий.
"""

import logging
import re
from dataclasses import dataclass

from ..config import settings
from ..constants import AuthResult, InboundAuthPolicy

logger = logging.getLogger(__name__)

#: Результаты вида `spf=pass`, `dkim=fail`, `dmarc=pass` в заголовке RFC 8601.
#: Отрицательный просмотр назад обязателен: `\b` срабатывает и внутри
#: `policy.dmarc=reject`, из-за чего заявленная политика затирала бы результат.
_METHOD = re.compile(r"(?<![.\w-])(spf|dkim|dmarc)=([a-z]+)", re.I)
#: Заявленная доменом политика DMARC: `policy.dmarc=reject`.
_POLICY = re.compile(r"\bpolicy\.dmarc=([a-z]+)", re.I)

MAX_HEADER_LENGTH = 2000


def _force_utf8_public_suffix_list() -> None:
    """Заставляет `authheaders` читать список доменов как UTF-8.

    Библиотека открывает `public_suffix_list.txt` без указания кодировки, а
    `open()` берёт кодировку системы — на русской Windows это cp1251. Список
    содержит интернационализированные домены в UTF-8, поэтому чтение падает с
    UnicodeDecodeError, разбор DMARC срывается, и — из-за защитного `except` в
    `verify()` — каждое входящее письмо молча принимается непроверенным. Отказ
    невидим: в журнале одна строка WARNING, а проверки просто не работают.

    Правится здесь, а не переменной окружения `PYTHONUTF8`: её пришлось бы
    задавать в каждом способе запуска (`run.bat`, `test.bat`, служба Windows,
    ручной вызов), и любой забытый превращал бы проверку подлинности в
    видимость проверки.
    """
    from authheaders import dmarc_lookup

    if getattr(dmarc_lookup, "_mg_utf8_patched", False):
        return

    def utf8_aware(location, domain):
        # Класс берём из самой библиотеки: она пробует `publicsuffix2`, затем
        # `publicsuffix`, и какой из них установлен — знает только она.
        return dmarc_lookup.PublicSuffixList(
            open(location, encoding="utf-8")
        ).get_public_suffix(domain)

    dmarc_lookup.get_org_domain_from_suffix_list = utf8_aware
    dmarc_lookup._mg_utf8_patched = True
    logger.debug("Список публичных суффиксов читается как UTF-8 независимо от локали")


@dataclass(frozen=True)
class AuthVerdict:
    """Итог проверки. `header` — готовая строка Authentication-Results."""

    spf: str = AuthResult.NONE
    dkim: str = AuthResult.NONE
    dmarc: str = AuthResult.NONE
    policy: str = ""
    header: str = ""

    @property
    def should_reject(self) -> bool:
        """Отклонять только то, что домен отправителя сам просит отклонять.

        Собственную политику поверх чужой не выдумываем: `p=reject` в DMARC —
        это явное указание владельца домена, а `p=quarantine` и `p=none`
        означают «решай сам», и мы решаем принять, отметив результат.
        """
        return self.dmarc == AuthResult.FAIL and self.policy == "reject"

    @property
    def suspicious(self) -> bool:
        return AuthResult.FAIL in (self.spf, self.dkim, self.dmarc)


#: Пустой вердикт — когда проверки выключены или невозможны.
UNCHECKED = AuthVerdict()


def verify(raw: bytes, *, peer_ip: str | None, mail_from: str, helo: str | None) -> AuthVerdict:
    """Проверяет письмо и возвращает вердикт. Никогда не бросает исключений.

    Сбой самой проверки (недоступен DNS, битые заголовки) не должен приводить
    к потере письма — в этом случае возвращается пустой вердикт, а письмо
    принимается и помечается непроверенным.
    """
    if settings.inbound_auth is InboundAuthPolicy.OFF:
        return UNCHECKED

    if not peer_ip:
        # Без адреса подключившегося SPF проверять не по чему. Обычно это
        # значит, что шлюз стоит за прокси без PROXY protocol.
        logger.warning("Адрес отправителя неизвестен, проверка SPF/DMARC пропущена")
        return UNCHECKED

    try:
        from authheaders import authenticate_message

        _force_utf8_public_suffix_list()
        header = authenticate_message(
            raw,
            settings.domain,
            prev=None,
            spf=True,
            dkim=True,
            dmarc=True,
            ip=peer_ip,
            mail_from=mail_from,
            helo=helo or "",
        )
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять приём
        logger.warning("Проверка подлинности не выполнена (%s): %s", type(exc).__name__, exc)
        return UNCHECKED

    return _parse(header)


def describe(verdict: AuthVerdict) -> str:
    return f"spf={verdict.spf} dkim={verdict.dkim} dmarc={verdict.dmarc}"


def _parse(header: str) -> AuthVerdict:
    found = {method.lower(): result.lower() for method, result in _METHOD.findall(header)}
    policy = _POLICY.search(header)
    return AuthVerdict(
        spf=found.get("spf", AuthResult.NONE),
        dkim=found.get("dkim", AuthResult.NONE),
        dmarc=found.get("dmarc", AuthResult.NONE),
        policy=policy.group(1).lower() if policy else "",
        header=header[:MAX_HEADER_LENGTH],
    )
