"""audiobook bookmarks

Places a listener marked in a book, per user and Plex Home profile, with
tombstones for deletes. New table only.

Revision ID: d7f3b8e41c62
Revises: c4e9a2d17f05
Create Date: 2026-09-28 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd7f3b8e41c62'
down_revision: str | None = 'c4e9a2d17f05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audiobook_bookmarks',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('profile', sa.String(length=64), server_default='', nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('plex_server_id', sa.String(length=64), nullable=False),
    sa.Column('rating_key', sa.String(length=32), nullable=False),
    sa.Column('position', sa.Float(), nullable=False),
    sa.Column('track_rating_key', sa.String(length=32), nullable=True),
    sa.Column('track_offset', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'profile', 'id')
    )
    op.create_index(
        'ix_audiobook_bookmarks_book',
        'audiobook_bookmarks',
        ['user_id', 'profile', 'plex_server_id', 'rating_key'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_audiobook_bookmarks_book', table_name='audiobook_bookmarks')
    op.drop_table('audiobook_bookmarks')
