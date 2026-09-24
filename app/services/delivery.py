"""Доставка исходящего письма во внешний мир: сборка MIME и передача по SMTP.

Транспорт и только транспорт: очередь, повторы и статусы — забота воркера.
"""

import email.utils
import logging
import mimetypes
import smtplib
import ssl
from email import policy
from email.message import EmailMessage

from ..config import settings
from ..constants import SMTP_PORT_MX, SMTP_TIMEOUT_SECONDS, OutboundMode
from ..db import utcnow
from ..models import Domain, Message
from ..utils import domain_of
from . import dkim_signer, storage
from .dkim_signer import DkimError

logger = logging.getLogger(__name__)


class PermanentSendError(Exception):
    """Ошибка, при которой повторять отправку бессмысленно (F-20)."""


def build_mime(message: Message) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = message.sender
    msg["To"] = message.recipient
    msg["Subject"] = message.subject or ""
    msg["Message-ID"] = message.message_id
    msg["Date"] = email.utils.format_datetime(utcnow())
    if message.in_reply_to:  # F-22
        msg["In-Reply-To"] = message.in_reply_to
        msg["References"] = message.thread_key or message.in_reply_to

    msg.set_content(message.body_text or "")
    if message.body_html:
        msg.add_alternative(message.body_html, subtype="html")

    for attachment in message.attachments:
        path = storage.readable(attachment.filepath)
        if path is None:
            logger.warning("Вложение %s недоступно, пропущено", attachment.filepath)
            continue
        mime_type = attachment.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(
            storage.read(path),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )

    return msg


def deliver(message: Message, domain: Domain | None) -> None:
    """Блокирующая отправка. Бросает PermanentSendError либо обычное исключение
    (последнее означает «стоит повторить»).

    `domain` — домен отправителя с его ключом DKIM; `None` — подписи не будет.
    """
    raw = serialize(build_mime(message), domain)
    if settings.outbound_mode is OutboundMode.RELAY:
        _deliver_via_relay(raw, message.sender, message.recipient)
    else:
        _deliver_direct(raw, message.sender, message.recipient)


def serialize(msg: EmailMessage, domain: Domain | None) -> bytes:
    """Готовые к передаче байты письма, уже подписанные DKIM.

    Сериализация здесь ровно одна, и подпись ложится на её результат. Если
    отдать транспорту объект письма и позволить `send_message()` собрать байты
    заново, заголовки свернутся иначе и подпись перестанет сходиться — письмо
    будет выглядеть подделанным, то есть хуже неподписанного.

    `policy.SMTP` даёт перевод строки CRLF, как того требуют и SMTP, и расчёт
    подписи.
    """
    raw = msg.as_bytes(policy=policy.SMTP)
    try:
        return dkim_signer.sign(raw, domain)
    except DkimError as exc:
        # Повторять бессмысленно: ключ не станет читаемым сам по себе, а тихая
        # отправка без подписи обесценила бы DMARC-политику домена.
        raise PermanentSendError(str(exc)) from exc


def _start_tls(server: smtplib.SMTP, *, required: bool, context: ssl.SSLContext) -> None:
    """Одинаковый разговор с сервером для обоих режимов.

    Контекст передаётся явно и всегда. Без него smtplib берёт собственный,
    который не проверяет ни сертификат, ни имя узла — а по умолчанию это
    молчаливое поведение библиотеки, которое может измениться с версией
    Python. Что именно уместно проверять, решают вызывающие: у релея и у
    чужого MX ответы прямо противоположные.
    """
    server.ehlo()
    if server.has_extn("STARTTLS"):
        server.starttls(context=context)
        server.ehlo()
    elif required:
        raise PermanentSendError("Релей не поддерживает STARTTLS, а MG_RELAY_USE_TLS=true")


def _relay_context() -> ssl.SSLContext:
    """Проверяемый TLS для релея: по этому соединению уходят логин и пароль.

    Хост релея известен заранее и имеет нормальный сертификат, так что
    проверять есть что. Без проверки любой, кто способен вклиниться в
    соединение, предъявляет собственный сертификат и получает от `login()`
    учётные данные релея в открытом виде — то есть право рассылать почту от
    имени домена.
    """
    context = ssl.create_default_context()
    if not settings.relay_verify_tls:
        # Осознанное послабление под внутренний релей с самоподписанным
        # сертификатом. В интернет с этим выходить нельзя.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _mx_context() -> ssl.SSLContext:
    """Оппортунистический TLS для чужого MX — намеренно без проверки (RFC 7435).

    Проверять там нечего: у публичных MX сплошь и рядом самоподписанные
    сертификаты и имена, не совпадающие с MX-записью. Требование проверки не
    улучшило бы защиту, а превратило бы доставку в отказ — альтернатива здесь
    не «проверенный канал», а открытый текст, потому что на 25-м порту
    отправитель обязан принять и его.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _deliver_via_relay(raw: bytes, sender: str, recipient: str) -> None:
    with smtplib.SMTP(
        settings.relay_host,
        settings.relay_port,
        local_hostname=settings.smtp_name,
        timeout=SMTP_TIMEOUT_SECONDS,
    ) as server:
        _start_tls(server, required=settings.relay_use_tls, context=_relay_context())
        if settings.relay_username:
            if settings.relay_use_tls and not _is_encrypted(server):
                # Пароль в открытом виде не отдаём никогда, даже если сервер
                # это позволяет: перехватить его на этом участке тривиально.
                raise PermanentSendError(
                    "Релей не поднял TLS, а MG_RELAY_USE_TLS=true — "
                    "учётные данные по открытому каналу не отправляются"
                )
            server.login(settings.relay_username, settings.relay_password)
        server.sendmail(sender, [recipient], raw)
    logger.info("Письмо передано релею %s", settings.relay_host)


def _is_encrypted(server: smtplib.SMTP) -> bool:
    return isinstance(getattr(server, "sock", None), ssl.SSLSocket)


def _deliver_direct(raw: bytes, sender: str, recipient: str) -> None:
    errors: list[str] = []
    for host in _mx_hosts(domain_of(recipient)):
        try:
            with smtplib.SMTP(
                host,
                SMTP_PORT_MX,
                # Без явного имени smtplib подставит имя машины из getfqdn():
                # для принимающей стороны это признак спама, а иногда и отказ.
                local_hostname=settings.smtp_name,
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as server:
                _start_tls(server, required=False, context=_mx_context())
                refused = server.sendmail(sender, [recipient], raw)
            if refused:
                raise PermanentSendError(f"Получатель отклонён сервером {host}: {refused}")
            logger.info("Письмо доставлено на %s через %s", recipient, host)
            return
        except (smtplib.SMTPRecipientsRefused, PermanentSendError) as exc:
            raise PermanentSendError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — пробуем следующий MX по приоритету
            logger.warning("MX %s недоступен: %s", host, exc)
            errors.append(f"{host}: {exc}")

    raise RuntimeError("Ни один MX-сервер не принял письмо: " + "; ".join(errors))


def _mx_hosts(domain: str) -> list[str]:
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN as exc:
        raise PermanentSendError(f"Домен {domain} не существует") from exc
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers) as exc:
        raise PermanentSendError(f"У домена {domain} нет MX-записей: {exc}") from exc
    except Exception as exc:  # таймаут DNS: повторяем позже
        raise RuntimeError(f"Не удалось получить MX-записи для {domain}: {exc}") from exc

    hosts = [r.exchange.to_text().rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
    if not hosts:
        raise PermanentSendError(f"У домена {domain} нет MX-записей")
    return hosts
