"""action display title

Revision ID: d4b1f70a52c8
Revises: b8e41d7c9a02
Create Date: 2026-08-27 08:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd4b1f70a52c8'
down_revision: str | None = 'b8e41d7c9a02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill on purpose: NULL means "the name is the button
    # copy", which is exactly how every existing action already behaves.
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('display_title', sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_column('display_title')
