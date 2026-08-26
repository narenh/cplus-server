"""seerr url moves to the environment

Revision ID: b8e41d7c9a02
Revises: b8e3c47a1d92
Create Date: 2026-08-26 00:00:00.000000

The Seerr URL is no longer stored. It decides who is admin, so it is read from
``CPLUS_SEERR_URL`` and cannot be set by any request; what is left here is a
fingerprint of the instance the install was last serving under, used only to
detect a repoint across restarts.

The fingerprint starts NULL, so the first start after this migration reads as a
change and flushes ``admin_sessions`` and ``plex_token_sessions`` — every device
and browser reconnects once. That is deliberate: those rows were resolved
against a URL this migration is deleting, so nothing can now prove what they
were resolved against.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e41d7c9a02'
down_revision: str | None = 'b8e3c47a1d92'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seerr_url_fingerprint', sa.String(length=64), nullable=True))
        batch_op.drop_column('seerr_url')


def downgrade() -> None:
    # The URL itself is gone and cannot be recovered from the fingerprint. A
    # downgraded install lands on the sign-in page with an empty Seerr field,
    # which is that version's own first-run state.
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seerr_url', sa.String(length=512), nullable=True))
        batch_op.drop_column('seerr_url_fingerprint')
