"""seerr request webhook: a shared secret, and a record of what has been announced

Two things, both for the same feature: requests filed in Seerr's own UI instead
of through this service.

``config.seerr_webhook_secret`` is what Seerr presents in its ``Authorization``
header.  Null — the state every existing install upgrades into — means the
webhook is switched off and ``POST /webhooks/seerr`` refuses everyone, so the
endpoint appears with nothing listening at it until an admin generates a secret.

``seerr_request_notices`` is one row per request this service has already logged
and pushed about, written by both ``POST /request`` and the webhook.  It starts
empty on purpose: requests filed before this migration were announced when they
happened, and Seerr will not notify about them again.

Revision ID: c4d9b7e21f06
Revises: b3e7d21a9c40
Create Date: 2026-09-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d9b7e21f06'
down_revision: str | None = 'b3e7d21a9c40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seerr_webhook_secret', sa.String(length=128), nullable=True))

    op.create_table(
        'seerr_request_notices',
        sa.Column('seerr_request_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('seerr_request_id'),
    )


def downgrade() -> None:
    op.drop_table('seerr_request_notices')

    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('seerr_webhook_secret')
