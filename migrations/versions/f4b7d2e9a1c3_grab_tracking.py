"""grab tracking: qBittorrent settings, a master switch, and per-grab tracking state

Every existing install starts with ``grab_tracking_enabled`` off and no
qBittorrent configured, so grabs behave exactly as before. The four ``grabs``
columns are null for every existing row and for every grab made while tracking
is off.

Revision ID: f4b7d2e9a1c3
Revises: e2c8f4a91d57
Create Date: 2026-10-20 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f4b7d2e9a1c3'
down_revision: str | None = 'e2c8f4a91d57'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qbittorrent_url', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('qbittorrent_username', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('qbittorrent_password', sa.String(length=256), nullable=True))
        batch_op.add_column(
            sa.Column('grab_tracking_enabled', sa.Boolean(), server_default='0', nullable=False)
        )

    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('torrent_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('tracking_phase', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('rating_key', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('tracking_error', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.drop_column('tracking_error')
        batch_op.drop_column('rating_key')
        batch_op.drop_column('tracking_phase')
        batch_op.drop_column('torrent_hash')

    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('grab_tracking_enabled')
        batch_op.drop_column('qbittorrent_password')
        batch_op.drop_column('qbittorrent_username')
        batch_op.drop_column('qbittorrent_url')
