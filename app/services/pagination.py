"""Постраничная выборка — общая для всех коллекций.

Раньше `mailboxes.get_page` и `messages.get_page` повторяли одну и ту же пару
запросов (страница + count) почти дословно. Здесь — один раз.
"""

from sqlalchemy import Select
from sqlalchemy.orm import Session

from ..schemas import PageResult


def paginate(db: Session, ordered_stmt: Select, count_stmt: Select, *, limit: int, offset: int) -> PageResult:
    """`ordered_stmt` — уже с ORDER BY (сортировка своя у каждой коллекции),
    `count_stmt` — тот же WHERE, но `select(func.count()).select_from(...)`."""
    items = list(db.scalars(ordered_stmt.offset(offset).limit(limit)))
    total = db.scalar(count_stmt) or 0
    return PageResult(items=items, total=total, limit=limit, offset=offset)
