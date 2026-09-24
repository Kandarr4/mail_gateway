"""Клиенты шлюза. Про HTTP не знает — принимает данные, отдаёт модели."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.errors import NotFound, ValidationError
from ..models import Client
from ..schemas import PageParams, PageResult
from .pagination import paginate


def by_id(db: Session, client_id: int) -> Client | None:
    return db.get(Client, client_id)


def get_page(db: Session, params: PageParams) -> PageResult:
    return paginate(
        db,
        select(Client).order_by(Client.name).offset(params.offset).limit(params.limit),
        select(func.count()).select_from(Client),
        limit=params.limit,
        offset=params.offset,
    )


def create(db: Session, name: str) -> Client:
    name = name.strip()
    if not name:
        raise ValidationError("Имя клиента не может быть пустым")
    client = Client(name=name)
    db.add(client)
    db.flush()
    return client


def set_active(db: Session, client_id: int, is_active: bool) -> Client:
    client = by_id(db, client_id)
    if client is None:
        raise NotFound("Клиент не найден")
    client.is_active = is_active
    return client


def delete(db: Session, client: Client) -> None:
    db.delete(client)
