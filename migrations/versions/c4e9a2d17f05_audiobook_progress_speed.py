"""audiobook progress speed

The playback rate a listener chose for a book, synced with their position.

Revision ID: c4e9a2d17f05
Revises: b81d3c07e2a9
Create Date: 2026-09-28 12:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4e9a2d17f05'
down_revision: str | None = 'b81d3c07e2a9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('audiobook_progress') as batch_op:
        batch_op.add_column(sa.Column('speed', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('audiobook_progress') as batch_op:
        batch_op.drop_column('speed')
