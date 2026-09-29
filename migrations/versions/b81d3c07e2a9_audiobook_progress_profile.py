"""audiobook progress per profile

Plex Home profiles share one account token, so progress gains a ``profile``
column in its key. Existing rows become the account owner's (``""``).

Revision ID: b81d3c07e2a9
Revises: a634b1eb30b1
Create Date: 2026-09-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b81d3c07e2a9'
down_revision: str | None = 'a634b1eb30b1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create(name: str, key: list[str], with_profile: bool) -> None:
    # Built by hand rather than batch_alter_table: batch mode can't change a
    # primary key without SQLAlchemy warning that it will one day refuse to.
    op.create_table(
        name,
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plex_server_id', sa.String(length=64), nullable=False),
        sa.Column('rating_key', sa.String(length=32), nullable=False),
        sa.Column('position', sa.Float(), nullable=False),
        sa.Column('track_rating_key', sa.String(length=32), nullable=True),
        sa.Column('track_offset', sa.Float(), nullable=True),
        sa.Column('finished', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('listened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('device', sa.String(length=128), nullable=True),
        *(
            [sa.Column('profile', sa.String(length=64), server_default='', nullable=False)]
            if with_profile
            else []
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint(*key),
    )


_COLUMNS = (
    "user_id, plex_server_id, rating_key, position, track_rating_key, track_offset,"
    " finished, listened_at, updated_at, device"
)


def upgrade() -> None:
    _create(
        '_audiobook_progress_new',
        ['user_id', 'plex_server_id', 'rating_key', 'profile'],
        with_profile=True,
    )
    op.execute(
        f"INSERT INTO _audiobook_progress_new ({_COLUMNS})"
        f" SELECT {_COLUMNS} FROM audiobook_progress"
    )
    op.drop_table('audiobook_progress')
    op.rename_table('_audiobook_progress_new', 'audiobook_progress')


def downgrade() -> None:
    _create('_audiobook_progress_old', ['user_id', 'plex_server_id', 'rating_key'], False)
    op.execute(
        f"INSERT INTO _audiobook_progress_old ({_COLUMNS})"
        f" SELECT {_COLUMNS} FROM audiobook_progress WHERE profile = ''"
    )
    op.drop_table('audiobook_progress')
    op.rename_table('_audiobook_progress_old', 'audiobook_progress')
