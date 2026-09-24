"""Убрана очередь вебхуков: единственный канал уведомлений — опрос API

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_queue")
        batch_op.drop_index(batch_op.f("ix_webhook_delivery_status"))
        batch_op.drop_index(batch_op.f("ix_webhook_delivery_next_attempt_at"))

    op.drop_table("webhook_delivery")


def downgrade() -> None:
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_webhook_delivery_next_attempt_at"), ["next_attempt_at"])
        batch_op.create_index(batch_op.f("ix_webhook_delivery_status"), ["status"])
        batch_op.create_index("ix_webhook_queue", ["status", "next_attempt_at"])
