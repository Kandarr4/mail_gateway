"""multi tenancy

Клиенты, домены и API-ключи как сущности БД вместо плоского списка токенов в
окружении. `mailbox`/`upload` получают обязательный `client_id`.

Добавление колонки сразу как NOT NULL упало бы на таблице с уже существующими
строками (dev-база в этой рабочей копии их содержит) — колонка добавляется
nullable, существующие строки привязываются к заведённому здесь клиенту
"Default" (домен — из уже настроенного MG_DOMAIN), и только затем колонка
становится NOT NULL. На пустой базе шаг привязки — no-op.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20 22:54:18.386433
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_CLIENT_NAME = "Default"


def upgrade() -> None:
    op.create_table(
        'client',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('client', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_client_name'), ['name'], unique=False)

    op.create_table(
        'domain',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('dkim_private_key_file', sa.String(length=1024), nullable=True),
        sa.Column('dkim_selector', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='CASCADE', name='fk_domain_client_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('domain', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_domain_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_domain_name'), ['name'], unique=True)

    op.create_table(
        'api_key',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('token_prefix', sa.String(length=12), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='CASCADE', name='fk_api_key_client_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('api_key', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_key_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_api_key_token_hash'), ['token_hash'], unique=True)

    # client_id сперва nullable — на населённой таблице NOT NULL без данных упал бы.
    with op.batch_alter_table('mailbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('upload', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_id', sa.Integer(), nullable=True))

    # У письма владелец хранится прямо на строке и остаётся nullable: письмо,
    # принятое до регистрации получателя, не принадлежит никому.
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_message_client_id'), ['client_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_message_client_id', 'client', ['client_id'], ['id'], ondelete='SET NULL'
        )

    _backfill_default_client()

    with op.batch_alter_table('mailbox', schema=None) as batch_op:
        batch_op.alter_column('client_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_mailbox_client_id'), ['client_id'], unique=False)
        batch_op.create_index('ix_mailbox_client_id_address', ['client_id', 'address'], unique=False)
        batch_op.create_foreign_key(
            'fk_mailbox_client_id', 'client', ['client_id'], ['id'], ondelete='CASCADE'
        )

    with op.batch_alter_table('upload', schema=None) as batch_op:
        batch_op.alter_column('client_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_upload_client_id'), ['client_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_upload_client_id', 'client', ['client_id'], ['id'], ondelete='CASCADE'
        )


def downgrade() -> None:
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_constraint('fk_message_client_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_message_client_id'))
        batch_op.drop_column('client_id')

    with op.batch_alter_table('upload', schema=None) as batch_op:
        batch_op.drop_constraint('fk_upload_client_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_upload_client_id'))
        batch_op.drop_column('client_id')

    with op.batch_alter_table('mailbox', schema=None) as batch_op:
        batch_op.drop_constraint('fk_mailbox_client_id', type_='foreignkey')
        batch_op.drop_index('ix_mailbox_client_id_address')
        batch_op.drop_index(batch_op.f('ix_mailbox_client_id'))
        batch_op.drop_column('client_id')

    op.drop_table('api_key')
    op.drop_table('domain')
    op.drop_table('client')


def _backfill_default_client() -> None:
    """Существующие ящики/загрузки (если есть) привязываются к клиенту
    "Default", а тот — к домену из MG_DOMAIN. На пустой базе — no-op."""
    bind = op.get_bind()
    meta = sa.MetaData()
    client_t = sa.Table('client', meta, autoload_with=bind)
    domain_t = sa.Table('domain', meta, autoload_with=bind)
    mailbox_t = sa.Table('mailbox', meta, autoload_with=bind)
    upload_t = sa.Table('upload', meta, autoload_with=bind)
    message_t = sa.Table('message', meta, autoload_with=bind)

    orphans = 0
    for table in (mailbox_t, upload_t, message_t):
        orphans += bind.execute(
            sa.select(sa.func.count()).select_from(table).where(table.c.client_id.is_(None))
        ).scalar() or 0
    if not orphans:
        return

    from datetime import UTC, datetime

    from app.config import settings

    now = datetime.now(UTC)
    client_id = bind.execute(
        client_t.insert().values(name=DEFAULT_CLIENT_NAME, is_active=True, created_at=now)
    ).inserted_primary_key[0]
    bind.execute(
        domain_t.insert().values(
            client_id=client_id,
            name=settings.domain,
            is_active=True,
            dkim_private_key_file=settings.dkim_private_key_file or None,
            dkim_selector=settings.dkim_selector,
            created_at=now,
        )
    )
    bind.execute(mailbox_t.update().where(mailbox_t.c.client_id.is_(None)).values(client_id=client_id))
    bind.execute(upload_t.update().where(upload_t.c.client_id.is_(None)).values(client_id=client_id))
    bind.execute(message_t.update().where(message_t.c.client_id.is_(None)).values(client_id=client_id))
