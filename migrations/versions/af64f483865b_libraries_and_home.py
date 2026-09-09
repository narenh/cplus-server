"""libraries and home

Revision ID: af64f483865b
Revises: f2a91c6d40b7
Create Date: 2026-09-09 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'af64f483865b'
down_revision: str | None = 'f2a91c6d40b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty/off for every existing install, which is not a placeholder: no
    # default libraries or home shelves is exactly what every install already
    # has today, and the carousel defaults match the app's own out-of-the-box
    # settings. Nothing to backfill.
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plex_admin_token', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('plex_server_base_url', sa.String(length=512), nullable=True))
        batch_op.add_column(
            sa.Column('plex_server_client_identifier', sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column('plex_server_name', sa.String(length=256), nullable=True))
        batch_op.add_column(
            sa.Column('default_libraries', sa.JSON(), server_default='[]', nullable=False)
        )
        batch_op.add_column(
            sa.Column('home_shelves', sa.JSON(), server_default='[]', nullable=False)
        )
        batch_op.add_column(sa.Column('home_carousel', sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'home_carousel_enabled', sa.Boolean(), server_default='1', nullable=False
            )
        )
        batch_op.add_column(
            sa.Column(
                'home_carousel_include_on_deck',
                sa.Boolean(),
                server_default='0',
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('home_carousel_include_on_deck')
        batch_op.drop_column('home_carousel_enabled')
        batch_op.drop_column('home_carousel')
        batch_op.drop_column('home_shelves')
        batch_op.drop_column('default_libraries')
        batch_op.drop_column('plex_server_name')
        batch_op.drop_column('plex_server_client_identifier')
        batch_op.drop_column('plex_server_base_url')
        batch_op.drop_column('plex_admin_token')
