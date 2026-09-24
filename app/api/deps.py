"""Зависимости FastAPI: единица работы, аутентификация, ограничение частоты.

Каждое из этих правил живёт здесь в единственном экземпляре — роуты их только
подключают.
"""

import logging
from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import ApiKey, Client
from ..services import api_keys
from ..services.rate_limit import RateLimiter
from .errors import RateLimited, Unauthorized

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False, description="API-ключ клиента (панель /admin/clients)")
_limiter = RateLimiter(settings.rate_limit_per_minute)


def get_db() -> Iterator[Session]:
    """Единица работы запроса: успех — ровно один commit, ошибка — rollback.

    Благодаря этому ни один сервис и ни один роут не вызывает commit сам.
    Сама механика — в `db.session_scope`, здесь только обёртка в форму
    FastAPI-зависимости: два одинаковых блока commit/rollback неизбежно
    разъехались бы при первой правке одного из них.
    """
    with session_scope() as db:
        yield db


def verify_token(db: Session, raw: str | None) -> ApiKey:
    """Ключ ищется по хешу — точным попаданием по индексу, а не перебором
    списка. Энтропии 256-битного токена достаточно, чтобы не требовалась
    защита сравнения от тайминг-атак: найти совпадение перебором хешей
    неосуществимо вне зависимости от того, как быстро БД отвечает "не найдено".
    """
    if not raw:
        raise Unauthorized("Требуется заголовок Authorization: Bearer <ключ>")

    api_key = api_keys.by_hash(db, raw)
    if api_key is None or not api_key.is_active or not api_key.client.is_active:
        raise Unauthorized()
    return api_key


def require_api_key(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> ApiKey:
    api_key = verify_token(db, creds.credentials if creds else None)
    api_keys.touch_last_used(db, api_key)
    return api_key


def require_client(api_key: ApiKey = Depends(require_api_key)) -> Client:
    """Клиент, которому принадлежит предъявленный ключ — единица области
    видимости для всех данных, отдаваемых через API (F-54)."""
    return api_key.client


def rate_limit(request: Request, api_key: ApiKey = Depends(require_api_key)) -> None:
    """Лимит на ключ (а при его отсутствии — на адрес клиента).

    FastAPI кеширует результат зависимости на запрос, поэтому `require_api_key`
    выполняется ровно один раз, даже когда его использует и `rate_limit` на
    уровне роутера, и `require_client` в каждом обработчике.
    """
    key = f"key:{api_key.id}" if api_key else (request.client.host if request.client else "unknown")
    if not _limiter.allow(key):
        raise RateLimited(limit_per_minute=settings.rate_limit_per_minute)


#: Стандартный набор для защищённого эндпоинта: ключ + лимит частоты.
protected = [Depends(rate_limit)]
