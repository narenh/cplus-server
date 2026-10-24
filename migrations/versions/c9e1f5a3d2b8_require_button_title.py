"""require a button title

Every action now has its own button title; there is no falling back to the
name. Actions without one take their name, which is what they already showed.

Revision ID: c9e1f5a3d2b8
Revises: b4d2e9a7c1f3
Create Date: 2026-10-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c9e1f5a3d2b8'
down_revision: str | None = 'b4d2e9a7c1f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE actions SET display_title = name"
        " WHERE display_title IS NULL OR trim(display_title) = ''"
    )
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.alter_column('display_title', existing_type=sa.String(length=128), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.alter_column('display_title', existing_type=sa.String(length=128), nullable=True)
