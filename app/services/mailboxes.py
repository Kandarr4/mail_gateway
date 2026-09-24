"""Реестр локальных ящиков. Про HTTP не знает — принимает данные, отдаёт модели.

Идентификатор ящика в API — его адрес: он уникален, известен вызывающей
стороне и не требует предварительного запроса, чтобы узнать числовой `id`.

`client_id` — граница видимости (F-54): `None` значит «без фильтра» и
используется только веб-панелью (общий администратор без разделения по
клиентам) и приёмом почты (там ещё нет ни одного клиента — есть только
адрес получателя, и адрес глобально уникален, так что искать есть где).
Вызовы из HTTP API обязаны передавать реальный `client_id`.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.errors import ValidationError
from ..models import Mailbox
from ..schemas import MailboxIn, PageParams, PageResult
from . import domains
from .pagination import paginate


def normalize(address: str) -> str:
    """Единственное правило нормализации адреса на всё приложение (F-31)."""
    return address.strip().lower()


def by_address(db: Session, address: str, *, active_only: bool = False) -> Mailbox | None:
    stmt = select(Mailbox).where(Mailbox.address == normalize(address))
    if active_only:
        stmt = stmt.where(Mailbox.is_active.is_(True))
    return db.scalars(stmt).first()


def exists(db: Session, address: str) -> bool:
    return by_address(db, address, active_only=True) is not None


def get_page(db: Session, params: PageParams, client_id: int | None = None) -> PageResult:
    stmt = select(Mailbox)
    count_stmt = select(func.count()).select_from(Mailbox)
    if client_id is not None:
        stmt = stmt.where(Mailbox.client_id == client_id)
        count_stmt = count_stmt.where(Mailbox.client_id == client_id)
    return paginate(
        db, stmt.order_by(Mailbox.address), count_stmt, limit=params.limit, offset=params.offset
    )


def upsert(
    db: Session, address: str, payload: MailboxIn, client_id: int | None = None
) -> tuple[Mailbox, bool]:
    """Идемпотентно приводит ящик к заданному виду.

    Возвращает (ящик, создан_ли) — вызывающему это нужно, чтобы ответить
    `201 Created` или `200 OK`. Домен адреса обязан быть заранее
    зарегистрирован — иначе получилась бы почта на чужом домене без его
    ведома, или ящик, повисший без DKIM-подписи.

    `client_id`: из API — обязательный, чужой домен отклоняется. Из веб-панели
    (до появления там выбора клиента, Phase 4) — `None`, владелец берётся из
    того, кому уже принадлежит домен адреса.
    """
    from ..utils import domain_of

    domain = domains.by_name(db, domain_of(address))
    if domain is None or (client_id is not None and domain.client_id != client_id):
        raise ValidationError(
            f"Домен адреса {address} не зарегистрирован за вашим клиентом",
            errors={"address": "домен не зарегистрирован"},
        )
    owner_id = client_id if client_id is not None else domain.client_id

    mailbox = by_address(db, address)
    if mailbox is not None and mailbox.client_id != owner_id:
        # Тому же адресу нельзя тайно сменить владельца через повторный upsert.
        raise ValidationError(f"Адрес {address} принадлежит другому клиенту")

    created = mailbox is None
    if mailbox is None:
        mailbox = Mailbox(client_id=owner_id, address=normalize(address))
        db.add(mailbox)

    mailbox.display_name = payload.display_name
    mailbox.external_id = payload.external_id
    mailbox.is_active = payload.is_active
    db.flush()
    return mailbox, created


def delete(db: Session, mailbox: Mailbox) -> None:
    db.delete(mailbox)
