"""home document modified_at

Revision ID: c7a29e1f4b83
Revises: d3f8a6c2e914
Create Date: 2026-09-10 05:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c7a29e1f4b83'
down_revision: str | None = 'd3f8a6c2e914'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no backfill: None means "never edited", the same role
    # CanopyPlus's own HomeSettings.modifiedAt gives .distantPast, and every
    # existing install's Home config has never been written through this
    # column. One stamp for the whole Home document (home_shelves,
    # home_carousel*, home_top_shelf together), not one per shelf — see
    # cplus_service.db.models.Config.home_modified_at.
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('home_modified_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('home_modified_at')
