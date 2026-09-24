"""Учётные записи веб-панели: аутентификация и управление."""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.errors import Conflict, NotFound, ValidationError
from ..db import utcnow
from ..models import AdminUser
from .passwords import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 10


def normalize(username: str) -> str:
    return username.strip().lower()


def by_username(db: Session, username: str) -> AdminUser | None:
    return db.scalars(select(AdminUser).where(AdminUser.username == normalize(username))).first()


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(AdminUser)) or 0


def authenticate(db: Session, username: str, password: str) -> AdminUser | None:
    """Возвращает пользователя либо None. Причину неудачи наружу не сообщает.

    Различать «нет такого пользователя» и «неверный пароль» нельзя: это
    позволило бы перебором выяснить существующие имена.
    """
    user = by_username(db, username)
    stored = user.password_hash if user else None

    if not verify_password(password, stored):
        logger.warning("Неудачная попытка входа: %s", normalize(username))
        return None
    if user is None or not user.is_active:
        logger.warning("Вход в отключённую учётную запись: %s", normalize(username))
        return None

    user.last_login_at = utcnow()
    # Параметры стоимости могли ужесточиться с момента последней смены пароля.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        logger.info("Хеш пароля %s пересчитан по текущим параметрам", user.username)
    return user


def create(db: Session, username: str, password: str) -> AdminUser:
    name = normalize(username)
    if not name:
        raise ValidationError("Имя пользователя не может быть пустым")
    if by_username(db, name) is not None:
        raise Conflict(f"Пользователь {name} уже существует")

    validate_password(password)
    user = AdminUser(username=name, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    logger.info("Создана учётная запись %s", name)
    return user


def set_password(db: Session, username: str, password: str) -> AdminUser:
    user = by_username(db, username)
    if user is None:
        raise NotFound(f"Пользователь {username} не найден")

    validate_password(password)
    user.password_hash = hash_password(password)
    logger.info("Пароль учётной записи %s изменён", user.username)
    return user


def validate_password(password: str) -> None:
    """Единственное место, где описаны требования к паролю."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Пароль короче {MIN_PASSWORD_LENGTH} символов")
    if password.isdigit() or password.isalpha():
        raise ValidationError("Пароль должен содержать и буквы, и цифры")
