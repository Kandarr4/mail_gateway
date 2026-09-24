"""Сессии веб-панели и защита от CSRF.

Сессии хранятся на стороне сервера, наружу уходит только случайный
идентификатор. Так выход действительно завершает сессию, а не полагается на
то, что клиент выбросит подписанный cookie.
"""

import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Request, Response

from ..config import settings
from ..db import utcnow

logger = logging.getLogger(__name__)

COOKIE_NAME = "mg_session"
CSRF_FIELD = "csrf_token"
#: Больше этого числа живых сессий не держим — защита от роста памяти.
MAX_SESSIONS = 1000


@dataclass
class Session:
    user_id: int
    username: str
    csrf: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return utcnow() >= self.expires_at


_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def create(response: Response, user_id: int, username: str) -> Session:
    token = secrets.token_urlsafe(32)
    session = Session(
        user_id=user_id,
        username=username,
        csrf=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(hours=settings.web_session_hours),
    )

    with _lock:
        _drop_expired()
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda key: _sessions[key].expires_at)
            del _sessions[oldest]
        _sessions[token] = session

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.web_session_hours * 3600,
        httponly=True,          # недоступна из JavaScript
        samesite="strict",      # cookie не уйдёт при переходе с чужого сайта
        secure=settings.web_secure_cookies,
        path="/admin",
    )
    return session


def current(request: Request) -> Session | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    with _lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if session.expired:
            del _sessions[token]
            return None
    return session


def destroy(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with _lock:
            _sessions.pop(token, None)
    response.delete_cookie(COOKIE_NAME, path="/admin")


def check_csrf(session: Session, submitted: str | None) -> bool:
    """Сверяет токен формы с токеном сессии за постоянное время.

    Сравнение в байтах, а не в строках: `compare_digest` на строках с
    не-ASCII символами бросает TypeError, и подставленный в форму кириллический
    токен обернулся бы ошибкой 500 вместо честного отказа.
    """
    if not submitted:
        return False
    return secrets.compare_digest(session.csrf.encode("utf-8"), submitted.encode("utf-8"))


def clear_all() -> None:
    with _lock:
        _sessions.clear()


def _drop_expired() -> None:
    for token in [t for t, s in _sessions.items() if s.expired]:
        del _sessions[token]
