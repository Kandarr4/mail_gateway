"""bounce reports

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bounced_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('bounce_status', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('bounce_diagnostic', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_column('bounce_diagnostic')
        batch_op.drop_column('bounce_status')
        batch_op.drop_column('bounced_at')
