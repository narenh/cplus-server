"""relay enrollment replaces the hand-entered url and key

The relay URL was a text box that only ever held one value, and the API key was
a credential an admin had to obtain out of band and paste in.  Both are gone:
the URL is a constant (with an environment override for development), and the
key is obtained automatically by enrolling with the relay the moment
notifications are switched on.

``notification_relay_url`` is dropped rather than kept-and-ignored, because a
stored URL that nothing reads is how a later reader concludes the setting still
works.  Anyone who genuinely needs a different relay sets ``CPLUS_RELAY_URL``.

Existing keys are **kept**.  A key entered by hand under the old scheme is
exactly as valid as one from enrollment — same derivation, same relay — so an
install that already had notifications working keeps working, and never enrols.
Only ``notification_relay_instance_id`` is left null for those, which costs
nothing: it is display-only, and the Notifications tab says so rather than
inventing one.

Revision ID: b8e3c47a1d92
Revises: a4f7d21e9c05
Create Date: 2026-08-26 12:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e3c47a1d92'
down_revision: str | None = 'a4f7d21e9c05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'notification_relay_instance_id', sa.String(length=64), nullable=True
            )
        )
        batch_op.drop_column('notification_relay_url')


def downgrade() -> None:
    """Restore the URL column, empty.

    Null is the right value to come back to: the older code reads it as "use
    the default relay", which is where every install was pointed anyway.
    """
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('notification_relay_url', sa.String(length=512), nullable=True)
        )
        batch_op.drop_column('notification_relay_instance_id')
