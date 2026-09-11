"""action sort order and icon

Revision ID: e7c04b915d38
Revises: a1d6f30b24e9
Create Date: 2026-09-11 07:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e7c04b915d38'
down_revision: str | None = 'a1d6f30b24e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'sort_order',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )
        # Nullable with no backfill: NULL means "the client picks", which is
        # what every action did before there was a column to say otherwise.
        batch_op.add_column(sa.Column('icon', sa.String(length=64), nullable=True))

    # Backfilled into the order clients were already being sent — the built-in
    # Request action first, then the rest by id — so an upgrade does not
    # silently rearrange anyone's buttons. Leaving every row at 0 would order
    # them by id alone and move Request to wherever its id falls.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id FROM actions ORDER BY is_system DESC, id")
    ).fetchall()
    for rank, (action_id,) in enumerate(rows):
        connection.execute(
            sa.text("UPDATE actions SET sort_order = :rank WHERE id = :id"),
            {"rank": rank, "id": action_id},
        )


def downgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_column('icon')
        batch_op.drop_column('sort_order')
