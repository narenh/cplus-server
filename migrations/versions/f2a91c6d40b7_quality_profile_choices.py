"""quality profile choices

Revision ID: f2a91c6d40b7
Revises: d4b1f70a52c8
Create Date: 2026-08-27 09:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f2a91c6d40b7'
down_revision: str | None = 'd4b1f70a52c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty for every existing profile, which is not a placeholder: no choices
    # means one undifferentiated pool ranked by the preference rules, exactly
    # how those profiles already behave. Nothing to backfill.
    with op.batch_alter_table('quality_profiles', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('choices', sa.JSON(), server_default='[]', nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table('quality_profiles', schema=None) as batch_op:
        batch_op.drop_column('choices')
