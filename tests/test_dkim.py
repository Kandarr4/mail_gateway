"""Подпись исходящих писем DKIM.

Проверяется то, ради чего подпись существует: что принимающая сторона может
её сверить. Поэтому подписи здесь не просто присутствуют — они проверяются
той же библиотекой, что и на чужом сервере.

Ключ и селектор — свойства домена (`Domain`), а не инстанса: подпись чужим
ключом развалила бы DMARC-выравнивание. В тестах домен — несохранённый
объект модели: `sign()` читает только атрибуты и в БД не ходит.
"""

import dkim as dkimpy
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.constants import Direction, MessageStatus
from app.models import Domain, Message
from app.services import delivery, dkim_signer
from app.services.delivery import PermanentSendError
from app.services.dkim_signer import DkimError

SELECTOR = "test"
DOMAIN = "test-gw.kz"


@pytest.fixture
def dkim_key(tmp_path):
    """Настоящая пара ключей: подпись проверяется по-настоящему.

    Возвращает (запись DNS, домен с ключом).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "dkim.private"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    import base64

    record = f"v=DKIM1; k=rsa; p={base64.b64encode(public).decode()}"
    domain = Domain(name=DOMAIN, dkim_selector=SELECTOR, dkim_private_key_file=str(path))
    return record, domain


def outgoing(**overrides) -> Message:
    fields = {
        "direction": Direction.OUTGOING,
        "status": MessageStatus.QUEUED,
        "sender": f"sales@{DOMAIN}",
        "recipient": "client@outside.example",
        "subject": "Тема письма",
        "body_text": "Текст письма",
        "message_id": f"<out-dkim@{DOMAIN}>",
    }
    return Message(**{**fields, **overrides})


def verify(raw: bytes, record: str) -> bool:
    """Проверка подписи так, как её сделает принимающий сервер."""
    return dkimpy.verify(raw, dnsfunc=lambda _name, **_kw: record)


# --- Подпись ------------------------------------------------------------------- #


def test_signature_verifies(dkim_key):
    record, domain = dkim_key
    raw = delivery.serialize(delivery.build_mime(outgoing()), domain)

    assert raw.startswith(b"DKIM-Signature:")
    assert verify(raw, record) is True


def test_signature_covers_the_subject(dkim_key):
    """Подпись обязана ломаться при подмене темы — иначе она бесполезна."""
    record, domain = dkim_key
    raw = delivery.serialize(delivery.build_mime(outgoing()), domain)
    tampered = _replace_header(raw, b"Subject", "Subject: Подделка".encode())

    assert verify(tampered, record) is False


def test_signature_survives_cyrillic_and_attachments(dkim_key, tmp_path):
    """Кодирование кириллицы и вложений не должно расходиться с подписью."""
    from app.models import Attachment

    record, domain = dkim_key
    payload = tmp_path / "файл.txt"
    payload.write_bytes("содержимое".encode())

    message = outgoing(body_html="<p>Тело письма</p>")
    message.attachments.append(
        Attachment(filename="файл.txt", filepath=str(payload),
                   content_type="text/plain", size=payload.stat().st_size)
    )

    raw = delivery.serialize(delivery.build_mime(message), domain)
    assert verify(raw, record) is True


def test_unsigned_when_domain_has_no_key():
    domain = Domain(name=DOMAIN, dkim_selector=SELECTOR, dkim_private_key_file=None)
    raw = delivery.serialize(delivery.build_mime(outgoing()), domain)
    assert not raw.startswith(b"DKIM-Signature:")


def test_unsigned_when_domain_is_unknown():
    """Домен удалили, пока письмо стояло в очереди — доставка важнее подписи."""
    raw = delivery.serialize(delivery.build_mime(outgoing()), None)
    assert not raw.startswith(b"DKIM-Signature:")


# --- Отказы -------------------------------------------------------------------- #


def test_missing_key_file_is_a_permanent_error(tmp_path):
    """Тихая отправка без подписи обесценила бы DMARC-политику домена."""
    domain = Domain(
        name=DOMAIN, dkim_selector=SELECTOR,
        dkim_private_key_file=str(tmp_path / "нет.private"),
    )
    with pytest.raises(PermanentSendError):
        delivery.serialize(delivery.build_mime(outgoing()), domain)


def test_broken_key_fails_loudly(tmp_path):
    """Негодный ключ — ошибка подписи, а не молчаливое письмо без неё."""
    bad = tmp_path / "bad.private"
    bad.write_text("это не ключ")
    domain = Domain(name=DOMAIN, dkim_selector=SELECTOR, dkim_private_key_file=str(bad))

    with pytest.raises(DkimError):
        dkim_signer.sign(b"From: a@test-gw.kz\r\n\r\nbody\r\n", domain)


def test_replaced_key_file_is_picked_up_without_restart(tmp_path):
    """Замену файла ключа через панель нельзя откладывать до перезапуска.

    Кеш ключей учитывает mtime файла: старый ключ после замены — это письма,
    подпись которых не сойдётся с новой записью в DNS.
    """
    import time

    def keygen():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    path = tmp_path / "rotating.private"
    path.write_bytes(keygen())
    first = dkim_signer._private_key(str(path))

    time.sleep(0.05)  # mtime должен отличаться
    path.write_bytes(keygen())
    import os

    os.utime(path)  # на FAT/грубом mtime гарантируем изменение метки
    second = dkim_signer._private_key(str(path))

    assert first != second


# --- Запись для DNS ------------------------------------------------------------- #


def test_dns_record_names_the_selector():
    name, value = dkim_signer.dns_record(DOMAIN, "mail", "КЛЮЧ")

    assert name == f"mail._domainkey.{DOMAIN}"
    assert value.startswith("v=DKIM1; k=rsa; p=")


def _replace_header(raw: bytes, header: bytes, replacement: bytes) -> bytes:
    lines = raw.split(b"\r\n")
    return b"\r\n".join(
        replacement if line.startswith(header + b":") else line for line in lines
    )
