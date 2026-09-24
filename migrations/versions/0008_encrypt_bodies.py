"""encrypt bodies

Шифрование содержимого писем (`body_text`, `body_html`, `bounce_diagnostic`).
Схема не меняется — колонки остаются Text, зашифрованное значение отличается
префиксом `enc:`. Миграция данных: при заданном `MG_ENCRYPTION_KEY_FILE`
существующие открытые строки шифруются; без ключа — no-op, они останутся
читаемыми и после включения шифрования (тип-декоратор понимает оба вида).

Файлы вложений на диске задним числом не трогаются: они шифруются с момента
включения ключа, старые остаются читаемыми по отсутствию сигнатуры.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-21 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("body_text", "body_html", "bounce_diagnostic")


def upgrade() -> None:
    from app import crypto

    if crypto.fernet() is None:
        return
    _rewrite(crypto.encrypt_text, only_plaintext=True)


def downgrade() -> None:
    from app import crypto

    if crypto.fernet() is None:
        return
    _rewrite(crypto.decrypt_text, only_plaintext=False)


def _rewrite(transform, *, only_plaintext: bool) -> None:
    from app import crypto

    bind = op.get_bind()
    meta = sa.MetaData()
    message_t = sa.Table('message', meta, autoload_with=bind)

    rows = bind.execute(
        sa.select(message_t.c.id, *(message_t.c[name] for name in COLUMNS))
    ).all()
    for row in rows:
        values = {}
        for name in COLUMNS:
            value = getattr(row, name)
            if value is None:
                continue
            if only_plaintext and value.startswith(crypto.TEXT_PREFIX):
                continue
            values[name] = transform(value)
        if values:
            bind.execute(
                message_t.update().where(message_t.c.id == row.id).values(**values)
            )
