"""per-user home settings

Revision ID: d3f8a6c2e914
Revises: bbc432f21723
Create Date: 2026-09-10 04:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd3f8a6c2e914'
down_revision: str | None = 'bbc432f21723'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A row only ever exists once an admin opens that user's Home editor for
    # the first time (see cplus_service.api.routes.admin.user_home), so this
    # starts empty on every install, upgraded or fresh alike — nothing to
    # backfill.
    op.create_table(
        'user_home_settings',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('home_shelves', sa.JSON(), server_default='[]', nullable=False),
        sa.Column('home_carousel', sa.JSON(), nullable=True),
        sa.Column(
            'home_carousel_enabled', sa.Boolean(), server_default='1', nullable=False
        ),
        sa.Column(
            'home_carousel_include_on_deck',
            sa.Boolean(),
            server_default='0',
            nullable=False,
        ),
        sa.Column('home_top_shelf', sa.JSON(), nullable=True),
        sa.Column('home_modified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    op.drop_table('user_home_settings')
