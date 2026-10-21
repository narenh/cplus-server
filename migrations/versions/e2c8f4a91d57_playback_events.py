"""playback events: a master switch, and the webhook URLs events are forwarded to

``config.playback_events_enabled`` starts off for every existing install, so
``POST /playback/events`` refuses everything and ``GET /capabilities`` tells the
app not to send until an admin switches it on in the Playback tab.

``playback_webhooks`` starts empty. Events themselves are never stored: the
Playback tab's recent list lives in memory and is gone on restart, by design.

Revision ID: e2c8f4a91d57
Revises: d7f3b8e41c62
Create Date: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e2c8f4a91d57'
down_revision: str | None = 'd7f3b8e41c62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('playback_events_enabled', sa.Boolean(), server_default='0', nullable=False)
        )

    op.create_table(
        'playback_webhooks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('url'),
    )


def downgrade() -> None:
    op.drop_table('playback_webhooks')

    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('playback_events_enabled')
