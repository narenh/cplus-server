"""action confirm body

Revision ID: b3e7d21a9c40
Revises: e7c04b915d38
Create Date: 2026-09-16 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3e7d21a9c40'
down_revision: str | None = 'e7c04b915d38'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill: NULL means "the client words it", which is what
    # every action did before there was a column to say otherwise. Backfilling
    # the client's current sentence would freeze it here, where nobody could
    # change it and an app update could not improve it.
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('confirm_body', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_column('confirm_body')
