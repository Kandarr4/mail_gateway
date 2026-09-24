"""Домены клиентов. Имя домена уникально глобально: DKIM/SPF домена может
контролировать только одна сторона, поэтому два клиента не могут заявить
один и тот же домен."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.errors import Conflict, NotFound
from ..models import Domain


def normalize(name: str) -> str:
    return name.strip().lower()


def by_name(db: Session, name: str) -> Domain | None:
    return db.scalars(select(Domain).where(Domain.name == normalize(name))).first()


def by_id(db: Session, domain_id: int) -> Domain | None:
    return db.get(Domain, domain_id)


def for_client(db: Session, client_id: int) -> list[Domain]:
    return list(db.scalars(select(Domain).where(Domain.client_id == client_id).order_by(Domain.name)))


def count_all(db: Session) -> int:
    """Число зарегистрированных доменов — против лимита лицензии `max_domains`."""
    return db.scalar(select(func.count()).select_from(Domain)) or 0


def create(db: Session, client_id: int, name: str) -> Domain:
    name = normalize(name)
    if by_name(db, name) is not None:
        raise Conflict(f"Домен {name} уже зарегистрирован")
    domain = Domain(client_id=client_id, name=name)
    db.add(domain)
    db.flush()
    return domain


def set_dkim(db: Session, domain_id: int, *, private_key_file: str | None, selector: str) -> Domain:
    domain = by_id(db, domain_id)
    if domain is None:
        raise NotFound("Домен не найден")
    domain.dkim_private_key_file = private_key_file or None
    domain.dkim_selector = selector or "mail"
    return domain


def set_active(db: Session, domain_id: int, is_active: bool) -> Domain:
    domain = by_id(db, domain_id)
    if domain is None:
        raise NotFound("Домен не найден")
    domain.is_active = is_active
    return domain


def delete(db: Session, domain: Domain) -> None:
    db.delete(domain)
