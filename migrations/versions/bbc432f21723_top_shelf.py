"""top shelf

Revision ID: bbc432f21723
Revises: af64f483865b
Create Date: 2026-09-10 00:19:00.673817
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'bbc432f21723'
down_revision: str | None = 'af64f483865b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: every existing install has none configured yet,
    # same as home_carousel before it — ensure_default_top_shelf seeds the
    # real "Continue Watching" default at startup rather than here.
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('home_top_shelf', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('home_top_shelf')
