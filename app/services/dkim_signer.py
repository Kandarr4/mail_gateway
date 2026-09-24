"""Подпись исходящего письма DKIM.

Подпись доказывает принимающей стороне, что письмо действительно отправлено
владельцем домена и не изменилось в пути. Без неё DMARC-политика домена
опирается на один SPF, а он ломается при любой пересылке письма, потому что
проверяет адрес подключившегося сервера, а не автора.

Ключ и селектор — свойства домена (`Domain`), а не инстанса: при нескольких
клиентах подпись чужим ключом развалила бы DMARC-выравнивание. Домен без
ключа — письма уходят без подписи, как и раньше, но теперь по-доменно.
"""

import logging
from pathlib import Path

from ..config import settings
from ..models import Domain

logger = logging.getLogger(__name__)

#: Заголовки под подписью. Подписывать надо ровно то, подмена чего меняет смысл
#: письма: адресатов, тему, дату и идентификатор. Заголовки, которые
#: промежуточные серверы законно переписывают (`Received`, `Return-Path`),
#: включать нельзя — подпись развалится по дороге.
SIGNED_HEADERS = (b"from", b"to", b"subject", b"date", b"message-id")


class DkimError(Exception):
    """Подписать не удалось: ключ не читается или не подходит."""


def sign(raw: bytes, domain: Domain | None) -> bytes:
    """Возвращает письмо с заголовком `DKIM-Signature` впереди.

    Подписываются именно те байты, которые уйдут в сеть. Подписать объект
    письма и потом дать библиотеке сериализовать его заново нельзя: при
    повторной сборке заголовки переносятся и перекодируются иначе, подпись
    перестаёт сходиться, и письмо выглядит подделанным — хуже, чем неподписанное.

    `domain=None` (домен удалили, пока письмо стояло в очереди) — письмо
    уходит без подписи: доставка важнее подписи, о причине скажет журнал.
    """
    if domain is None or not domain.dkim_private_key_file:
        return raw

    import dkim

    try:
        signature = dkim.sign(
            message=raw,
            selector=domain.dkim_selector.encode(),
            domain=domain.name.encode(),
            privkey=_private_key(domain.dkim_private_key_file),
            include_headers=list(SIGNED_HEADERS),
        )
    except Exception as exc:  # причина уходит наверх одним типом
        raise DkimError(f"Не удалось подписать письмо DKIM: {exc}") from exc

    return signature + raw


#: Кеш ключей: путь -> (mtime, байты). Ключ кеша включает mtime, чтобы замена
#: файла ключа через панель подхватывалась без перезапуска процесса — у
#: `lru_cache` нет инвалидации, и застрявший старый ключ подписывал бы письма
#: до следующего рестарта.
_key_cache: dict[str, tuple[float, bytes]] = {}


def _private_key(path_str: str) -> bytes:
    path = Path(path_str)
    if not path.is_absolute():
        from ..config import BASE_DIR

        path = BASE_DIR / path
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise DkimError(
            f"Файл закрытого ключа DKIM не найден: {path}. "
            "Создайте ключ: python manage.py dkim-keygen"
        ) from exc

    cached = _key_cache.get(path_str)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    data = path.read_bytes()
    _key_cache[path_str] = (mtime, data)
    return data


def check_ready() -> None:
    """Проверка ключа по умолчанию на старте — до первого письма.

    Проверяется ключ из настроек инстанса (он же достаётся домену «Default»
    при бутстрапе). Ключи остальных доменов проверяются при сохранении в
    панели и при первом использовании.
    """
    if not settings.dkim_private_key_file:
        return
    _private_key(settings.dkim_private_key_file)
    logger.info(
        "DKIM: ключ по умолчанию читается, селектор %s, домен %s",
        settings.dkim_selector, settings.domain,
    )


def check_key_file(path_str: str) -> None:
    """Читаемость ключа для проверки из панели при сохранении домена."""
    _private_key(path_str)


def dns_record(domain_name: str, selector: str, public_key_base64: str) -> tuple[str, str]:
    """Имя и значение TXT-записи, которую владелец домена ставит в DNS."""
    return (
        f"{selector}._domainkey.{domain_name}",
        f"v=DKIM1; k=rsa; p={public_key_base64}",
    )
