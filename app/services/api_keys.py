"""API-ключи клиентов.

Хранится только SHA-256 хеш токена — не scrypt: ключ представляет собой
случайную строку с 256 битами энтропии, проверяемую на каждый HTTP-запрос, а
не пароль, подобранный человеком. Именно энтропия ключа — защита от перебора;
делать проверку скрипт-медленной означало бы платить ~50мс на каждый вызов
API (включая опрос по `since_id`, который клиент делает раз в несколько
секунд) без единой причины: онлайн-перебор 256-битного токена неосуществим
вне зависимости от скорости хеша.
"""

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import ApiKey

_TOKEN_BYTES = 32
_PREFIX_LEN = 8


def generate_raw_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def by_hash(db: Session, raw: str) -> ApiKey | None:
    return db.scalars(select(ApiKey).where(ApiKey.token_hash == hash_token(raw))).first()


def list_for_client(db: Session, client_id: int) -> list[ApiKey]:
    return list(
        db.scalars(select(ApiKey).where(ApiKey.client_id == client_id).order_by(ApiKey.created_at))
    )


def create(db: Session, client_id: int, name: str) -> tuple[ApiKey, str]:
    """Возвращает (запись, токен). Токен — единственный раз, дальше не восстановим."""
    raw = generate_raw_token()
    return create_with_raw(db, client_id, name, raw), raw


def create_with_raw(db: Session, client_id: int, name: str, raw: str) -> ApiKey:
    """Для бутстрапа из `MG_API_TOKENS`, где значение токена уже задано извне."""
    api_key = ApiKey(
        client_id=client_id,
        name=name,
        token_hash=hash_token(raw),
        token_prefix=raw[:_PREFIX_LEN],
    )
    db.add(api_key)
    db.flush()
    return api_key


def touch_last_used(db: Session, api_key: ApiKey) -> None:
    api_key.last_used_at = utcnow()


def revoke(db: Session, api_key: ApiKey) -> None:
    api_key.is_active = False
