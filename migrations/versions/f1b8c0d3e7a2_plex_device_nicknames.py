"""plex device registry and nicknames

Adds the ``plex_devices`` registry (identifier → admin nickname) and the
``device_identifier`` column that ``grabs`` and ``activity_log`` record it on.

**Nothing is backfilled, and nothing can be.** The identifier was never
received before this revision, so no historical row has one to recover — every
grab and log entry written before this deploy stays ``NULL`` for good, and the
admin console renders that as "unknown" rather than guessing. Rows start
appearing the first time a client sends ``X-Plex-Client-Identifier``.

Revision ID: f1b8c0d3e7a2
Revises: b3e7d21a9c40
Create Date: 2026-09-18 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1b8c0d3e7a2'
down_revision: str | None = 'b3e7d21a9c40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'plex_devices',
        sa.Column('client_identifier', sa.String(length=128), nullable=False),
        sa.Column('nickname', sa.String(length=64), nullable=True),
        sa.Column('device_name', sa.String(length=256), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('client_identifier'),
    )
    # No foreign key to ``plex_devices`` from either table: the device row holds
    # only a nickname, and removing one must not take the history with it.  See
    # ``Grab.device_identifier``.
    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_identifier', sa.String(length=128), nullable=True))

    with op.batch_alter_table('activity_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_identifier', sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activity_log', schema=None) as batch_op:
        batch_op.drop_column('device_identifier')

    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.drop_column('device_identifier')

    op.drop_table('plex_devices')
