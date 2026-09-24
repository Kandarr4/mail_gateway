"""SMTP-транспорт приёма почты. Тонкий: разбор и хранение — в `services.receiving`."""

import asyncio
import logging
import ssl

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import Session

from .config import settings
from .constants import PROXY_PROTOCOL_TIMEOUT, SMTP_COMMAND_LIMIT, InboundAuthPolicy
from .db import session_scope
from .services import licensing, mailboxes, receiving
from .services.rate_limit import RateLimiter
from .services.receiving import Outcome

logger = logging.getLogger(__name__)

# Ответы SMTP собраны здесь, чтобы коды не расползались по обработчикам.
_OK = "250 Message accepted for delivery"
_OK_DUPLICATE = "250 Message already accepted"
_NO_USER = "550 No such user here"
_LOCAL_ERROR = "451 Requested action aborted: local error in processing"
_NOT_AUTHENTIC = "550 5.7.1 Message rejected by sender domain policy (DMARC)"
_TOO_MANY = "421 4.7.0 Too many messages from your address, try later"
#: Нет действующей лицензии. Именно 4xx, а не 550: временный отказ отправитель
#: повторяет несколько дней, и письма дойдут после установки лицензии. Отказ
#: постоянным кодом вернул бы их авторам с пометкой «не существует».
_UNLICENSED = "451 4.3.2 Service not available, mail processing suspended"

_OUTCOME_REPLIES = {
    Outcome.STORED: _OK,
    Outcome.DUPLICATE: _OK_DUPLICATE,
    Outcome.UNKNOWN_MAILBOX: _NO_USER,
    Outcome.NOT_AUTHENTIC: _NOT_AUTHENTIC,
}

#: Адреса «слушать всё», которые aiosmtpd понимает только в виде пустой строки.
_BIND_ALL = {"0.0.0.0", "::", "*"}  # noqa: S104 — список адресов для сравнения, не привязка

_limiter = RateLimiter(settings.smtp_rate_limit_per_minute)


class EmailHandler:
    async def handle_PROXY(self, server, session, envelope, proxy_data) -> bool:  # noqa: N802 — имя задано aiosmtpd
        """Принять соединение от прокси, стоящего перед шлюзом.

        Метод обязателен при включённом `MG_PROXY_PROTOCOL`: aiosmtpd считает
        отсутствие этого хука отказом и закрывает соединение до приветствия —
        отправитель видит обрыв вместо `220`.

        Заголовку здесь верят на слово, и это осознанно: подтверждать его
        нечем, а защита строится тем, что до порта 25 дотягивается только сам
        прокси. Обеспечивается это правилом файрвола, а не проверкой в коде.
        Открытый всем порт с `MG_PROXY_PROTOCOL=true` — дыра: подключившийся
        назовётся чужим адресом и пройдёт SPF.
        """
        if proxy_data.error:
            logger.warning("Отклонён заголовок PROXY protocol: %s", proxy_data.error)
            return False
        logger.debug("PROXY protocol: соединение от %s через %s", proxy_data.src_addr, session.peer)
        return True

    async def handle_MAIL(self, server, session, envelope, address, mail_options):  # noqa: N802 — имя задано aiosmtpd
        """Лимит писем с одного адреса — раньше, чем принято тело."""
        # Лицензия — на первом же шаге конверта: тело неоплаченного письма
        # даже не принимается по сети.
        if not licensing.mail_allowed():
            logger.warning("Письмо от %s отклонено: %s", address, licensing.suspension_notice())
            return _UNLICENSED

        peer = client_ip(session)
        if not _limiter.allow(peer or "unknown"):
            logger.warning("Превышен лимит писем с адреса %s", peer)
            return _TOO_MANY
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802 — имя задано aiosmtpd
        """Неизвестный адрес отсекаем до приёма тела — трафик не тратится (F-02)."""
        if settings.require_known_mailbox and not await asyncio.to_thread(_mailbox_exists, address):
            logger.warning("Отклонён RCPT для неизвестного ящика: %s", address)
            return _NO_USER
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 — имя задано aiosmtpd
        try:
            result = await asyncio.to_thread(
                receiving.accept,
                envelope.content,
                list(envelope.rcpt_tos),
                envelope.mail_from,
                peer_ip=client_ip(session),
                helo=session.host_name,
            )
        except Exception:
            # 451 — временный отказ: отправитель повторит доставку позже (F-10).
            logger.exception("Ошибка обработки входящего письма от %s", envelope.mail_from)
            return _LOCAL_ERROR
        return _OUTCOME_REPLIES[result.outcome]


def client_ip(session: Session) -> str | None:
    """Адрес отправителя с учётом прокси перед шлюзом.

    Если включён PROXY protocol, настоящий адрес лежит в его заголовке, а
    `session.peer` указывает на прокси. Различие принципиально: по этому
    адресу проверяется SPF, и подстановка адреса прокси обесценила бы проверку.
    """
    if settings.proxy_protocol and session.proxy_data is not None:
        src = session.proxy_data.src_addr
        if src:
            return str(src)
    return session.peer[0] if session.peer else None


def _mailbox_exists(address: str) -> bool:
    with session_scope() as db:
        return mailboxes.exists(db, address)


def _bind_host() -> str:
    """Приводит адрес прослушивания к виду, пригодному для aiosmtpd.

    После запуска сервера контроллер сам подключается к нему, чтобы разбудить
    фабрику протокола. Подключение к `0.0.0.0` под Windows недопустимо
    (WinError 10049), и сервис падает при старте на конфигурации по умолчанию.
    Пустая строка означает то же «слушать на всех интерфейсах», но пробное
    подключение идёт на localhost.
    """
    return "" if settings.smtp_host.strip() in _BIND_ALL else settings.smtp_host


def tls_context() -> ssl.SSLContext | None:
    """Контекст для STARTTLS либо None, если сертификат не задан.

    Именно STARTTLS, а не TLS с первого байта: на 25-м порту соединение
    всегда начинается открытым текстом, и шифрование включается по команде
    отправителя. Требовать его нельзя — публичный MX обязан принимать почту
    и от серверов без поддержки TLS, иначе письма просто не дойдут.
    """
    paths = settings.tls_paths
    if paths is None:
        return None

    cert, key = paths
    missing = [str(p) for p in (cert, key) if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Не найдены файлы сертификата: {', '.join(missing)}")

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def start_smtp() -> Controller:
    context = tls_context()
    controller = Controller(
        EmailHandler(),
        hostname=_bind_host(),
        port=settings.smtp_port,
        # Потолок размера письма целиком: без него в память читается всё,
        # что пришлёт отправитель.
        data_size_limit=settings.max_message_bytes,
        enable_SMTPUTF8=True,  # адреса с кириллицей
        command_call_limit=SMTP_COMMAND_LIMIT,
        tls_context=context,
        require_starttls=False,
        proxy_protocol_timeout=PROXY_PROTOCOL_TIMEOUT if settings.proxy_protocol else None,
        # Имя в приветствии и в ответе на EHLO. По умолчанию aiosmtpd берёт
        # getfqdn(), то есть имя машины вроде WIN-SERVER.
        server_hostname=settings.smtp_name,
        ident=f"Mail Gateway ({settings.domain})",
    )
    controller.start()

    logger.info(
        "SMTP-сервер слушает %s:%s (STARTTLS: %s, PROXY protocol: %s, проверка отправителя: %s)",
        settings.smtp_host,
        settings.smtp_port,
        "включён" if context else "ВЫКЛЮЧЕН",
        "включён" if settings.proxy_protocol else "выключен",
        settings.inbound_auth,
    )
    if settings.inbound_auth is not InboundAuthPolicy.OFF and not settings.proxy_protocol:
        logger.info("SPF проверяется по адресу подключения; за прокси включите MG_PROXY_PROTOCOL")
    return controller
