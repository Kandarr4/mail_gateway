"""Какие записи DNS нужны домену — и что из них уже настроено.

Одно место, где собран весь набор: A, MX, SPF, DKIM, DMARC и обратная запись.
Раньше программа показывала только DKIM (его она сама и создаёт), а остальное
оператор искал в инструкции — и, как правило, не находил: письма уходили без
DMARC и с чужим PTR, то есть прямиком в спам.

Значения считаются из настроек и ключа домена, а не переписываются руками:
селектор DKIM, имя в HELO и порт живут в `.env`, и запись обязана меняться
вместе с ними.

Проверка (`check`) ходит в DNS и сравнивает то, что *должно* быть, с тем, что
*есть*. Она блокирующая (сетевые запросы с таймаутом) — вызывать из потока,
а не из обработчика интерфейса.
"""

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

#: Таймаут одного DNS-запроса и всего разрешения имени. Проверка идёт по
#: шести записям подряд, и минутное ожидание в диалоге недопустимо.
DNS_TIMEOUT_SECONDS = 3.0

#: Приоритет единственной записи MX. Значение произвольно, важно лишь то, что
#: оно одинаково у всех наших развёртываний — оператору не о чем думать.
MX_PRIORITY = 10

OK = "ok"
MISSING = "missing"
DIFFERENT = "different"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Record:
    """Одна требуемая запись DNS.

    `host` — то, что вписывают в поле «Хост» панели регистратора: имя
    относительно зоны (`@` — сам домен). Панели почти всегда дописывают зону
    сами, и полное имя в этом поле превращается в `mail._domainkey.example.kz
    .example.kz`. Полное имя лежит рядом (`fqdn`) — для проверки и для тех
    панелей, где просят именно его.
    """

    kind: str
    type: str
    host: str
    fqdn: str
    value: str
    note: str = ""
    #: Без записи почта не работает вовсе (в отличие от желательных).
    required: bool = True


@dataclass(frozen=True)
class Checked:
    """Результат сверки требуемой записи с тем, что отвечает DNS."""

    record: Record
    status: str
    actual: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


def mail_host(domain: str) -> str:
    """Имя, которым шлюз представляется и на которое смотрит MX.

    Совпадает с `settings.smtp_name` для домена инстанса; для прочих доменов
    клиента почта всё равно уходит через этот же сервер, поэтому и MX у них
    указывает сюда же.
    """
    return settings.smtp_name if domain == settings.domain else f"mail.{domain}"


def required_records(domain: str, *, dkim_public_key: str | None = None) -> list[Record]:
    """Полный список записей для домена. Сеть не трогает."""
    host = mail_host(domain)
    records = [
        Record(
            kind="Адрес почтового сервера",
            type="A",
            host=host.removesuffix(f".{domain}") if host.endswith(f".{domain}") else host,
            fqdn=host,
            value="<публичный IP-адрес этого сервера>",
            note="Адрес, по которому доступен сервер из интернета. "
                 "Именно это имя шлюз называет в SMTP (HELO).",
        ),
        Record(
            kind="Приём почты",
            type="MX",
            host="@",
            fqdn=domain,
            value=f"{MX_PRIORITY} {host}.",
            note="Куда другие серверы приносят письма для этого домена.",
        ),
        Record(
            kind="Разрешение на отправку (SPF)",
            type="TXT",
            host="@",
            fqdn=domain,
            value=f"v=spf1 a:{host} -all",
            note="Список тех, кому можно отправлять от имени домена. "
                 "Если записи ещё нет — добавьте эту; если есть — впишите "
                 f"a:{host} в существующую, вторая запись SPF недопустима.",
        ),
    ]

    key = dkim_public_key if dkim_public_key is not None else public_key_of(domain)
    records.append(
        Record(
            kind="Подпись писем (DKIM)",
            type="TXT",
            host=f"{settings.dkim_selector}._domainkey",
            fqdn=f"{settings.dkim_selector}._domainkey.{domain}",
            value=f"v=DKIM1; k=rsa; p={key}" if key else "",
            note="Открытый ключ подписи. Создаётся программой: "
                 f"MailGateway.exe dkim-keygen --domain {domain}"
                 if key else
                 "Ключ ещё не создан. Выполните: "
                 f"MailGateway.exe dkim-keygen --domain {domain}",
        )
    )

    records.append(
        Record(
            kind="Политика домена (DMARC)",
            type="TXT",
            host="_dmarc",
            fqdn=f"_dmarc.{domain}",
            value=f"v=DMARC1; p=none; rua=mailto:postmaster@{domain}",
            note="Что делать получателю, если письмо не прошло SPF и DKIM. "
                 "Начинать строго с p=none — это «ничего не делать, только "
                 "присылать отчёты»; ужесточать до quarantine и reject после "
                 "того, как в отчётах не останется своих же писем.",
        )
    )

    records.append(
        Record(
            kind="Обратная запись (PTR)",
            type="PTR",
            host="—",
            fqdn="<IP-адрес сервера>",
            value=host,
            note="Ставится НЕ в зоне домена, а владельцем IP-адреса — "
                 "провайдером или хостингом, заявкой в поддержку. Крупные "
                 "почтовые службы отвергают письма, у которых PTR не совпадает "
                 "с именем из HELO.",
        )
    )
    return records


def public_key_of(domain: str) -> str:
    """Открытый ключ DKIM домена в base64 — из его же закрытого ключа.

    Путь берётся у домена в базе, а при его отсутствии — из `.env` (домен
    инстанса). Ключа нет — пустая строка: значит, его ещё не создавали.
    """
    path = _key_path(domain)
    if path is None or not path.is_file():
        return ""
    try:
        from cryptography.hazmat.primitives import serialization

        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        der = private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception:  # битый или чужой файл
        logger.exception("Не удалось прочитать ключ DKIM домена %s", domain)
        return ""
    return base64.b64encode(der).decode()


def _key_path(domain: str) -> Path | None:
    from ..config import BASE_DIR

    if domain == settings.domain and settings.dkim_key_path is not None:
        return settings.dkim_key_path
    return BASE_DIR / "data" / "dkim" / f"{domain}.private"


MARKS = {
    OK: "[ OK ]",
    MISSING: "[ НЕТ ]",
    DIFFERENT: "[ ИНОЕ ]",
    UNKNOWN: "[  ?  ]",
}


def as_text(domain: str, checked: list["Checked"] | None = None) -> str:
    """Готовая памятка оператору — одна на все каналы.

    Её печатает `manage.py dns-records`, её же мастер первого запуска кладёт
    в `dns_record.txt` рядом с программой. Раньше в файле был один DKIM, и
    про MX, SPF, DMARC и PTR оператор узнавал в лучшем случае из инструкции.
    """
    records = [item.record for item in checked] if checked else required_records(domain)
    lines = [
        f"Записи DNS для домена {domain}",
        "",
        "Поле «Хост» в панели регистратора заполняется ОТНОСИТЕЛЬНО зоны:",
        f"  «@» — сам домен {domain}, «mail» превратится в mail.{domain}.",
        "Если панель показывает существующие записи полными именами —",
        "вписывайте полное имя из строки «полное имя».",
        "",
    ]
    for index, record in enumerate(records):
        state = f"{MARKS[checked[index].status]} " if checked else ""
        lines += [
            f"{index + 1}. {state}{record.kind}",
            f"     тип:          {record.type}",
            f"     хост:         {record.host}",
            f"     полное имя:   {record.fqdn}",
            f"     значение:     {record.value or '(ещё не создано)'}",
        ]
        if checked and checked[index].actual:
            lines.append(f"     сейчас в DNS: {checked[index].actual}")
        lines += [f"     зачем:        {record.note}", ""]

    if checked:
        bad = [item for item in checked if not item.ok]
        lines.append("Всё на месте." if not bad else f"Требует внимания записей: {len(bad)}.")
    else:
        lines.append("Проверить, что уже настроено: MailGateway.exe dns-records --check")
    return "\n".join(lines) + "\n"


# --- Сверка с тем, что отвечает DNS --------------------------------------------- #


def check(records: list[Record]) -> list[Checked]:
    """Спрашивает DNS по каждой записи. Блокирующая — только из потока."""
    return [_check_one(record) for record in records]


def _check_one(record: Record) -> Checked:
    import dns.resolver

    try:
        actual = _lookup(record)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        # «Такого имени нет» — это ответ, а не сбой связи: запись не добавлена.
        return Checked(record, MISSING)
    except Exception:  # noqa: BLE001 — таймаут, нет сети, кривой резолвер
        logger.debug("Проверка DNS не удалась: %s %s", record.type, record.fqdn, exc_info=True)
        return Checked(record, UNKNOWN)

    if not actual:
        return Checked(record, MISSING)
    return Checked(record, OK if _matches(record, actual) else DIFFERENT, "; ".join(actual))


def _lookup(record: Record) -> list[str]:
    import dns.resolver
    import dns.reversename

    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS

    if record.type == "PTR":
        # Адрес сервера заранее неизвестен: берём тот, на который указывает
        # наше же имя — принимающая сторона придёт ровно к нему.
        addresses = [str(item) for item in resolver.resolve(record.value, "A")]
        if not addresses:
            return []
        name = dns.reversename.from_address(addresses[0])
        return [str(item).rstrip(".") for item in resolver.resolve(name, "PTR")]

    if record.type == "TXT":
        return [
            b"".join(item.strings).decode("utf-8", "replace")
            for item in resolver.resolve(record.fqdn, "TXT")
        ]

    if record.type == "MX":
        return [
            f"{item.preference} {str(item.exchange).rstrip('.')}"
            for item in resolver.resolve(record.fqdn, "MX")
        ]

    return [str(item) for item in resolver.resolve(record.fqdn, record.type)]


def _matches(record: Record, actual: list[str]) -> bool:
    """Совпадение по существу, а не побуквенно.

    Оператор вправе написать SPF по-своему (`+a`, `ip4:` вместо `a:`), MX — с
    другим приоритетом, а DMARC — с отчётами на другой адрес. Ругаться на это
    значило бы гонять человека по кругу ради нашего вкуса, поэтому проверяем
    то, без чего запись не работает.
    """
    if record.type == "A":
        return True  # адрес сервера знает только оператор — сверять не с чем

    if record.type == "PTR":
        return any(item.lower() == record.value.lower() for item in actual)

    if record.type == "MX":
        target = record.value.split(" ", 1)[1].rstrip(".").lower()
        return any(item.split(" ", 1)[1].lower() == target for item in actual)

    if record.fqdn.startswith("_dmarc."):
        return any(item.lower().startswith("v=dmarc1") for item in actual)

    if "._domainkey." in record.fqdn:
        expected = record.value.split("p=", 1)[-1]
        return bool(expected) and any(expected in item.replace(" ", "") for item in actual)

    # SPF: запись одна, и в ней должно быть разрешение нашему серверу.
    host = mail_host(record.fqdn).lower()
    for item in actual:
        low = item.lower()
        if not low.startswith("v=spf1"):
            continue
        return f"a:{host}" in low or " +a" in f" {low}" or " a " in f" {low} " or "mx" in low
    return False
