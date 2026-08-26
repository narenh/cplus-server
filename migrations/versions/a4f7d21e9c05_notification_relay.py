"""notification relay replaces local apns credentials

This install never had a legitimate way to hold an APNs signing key: the key
belongs to the Apple Developer account that owns the app and signs pushes for
that whole team, so it cannot be handed to every self-hoster.  Pushes now go
through a forwarding relay that holds it, and the four local credential columns
go away.

``apns_private_key`` is **dropped, not migrated**.  Anything that was pasted
into it was either the wrong file or a key its owner should now rotate, and
carrying it forward would mean a secret sitting in the config row that nothing
reads.

``notifications_enabled`` lands ``False`` for everyone, including installs that
had push working before this upgrade.  That is not a mistake and not a
regression to fix: turning it on routes notification text through a third party
in plaintext, and no migration gets to make that decision on an admin's behalf.
Existing device registrations and per-type switches are kept, so an admin who
does turn it on finds their configuration where they left it.

Revision ID: a4f7d21e9c05
Revises: 803cf5974bf3
Create Date: 2026-08-26 11:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a4f7d21e9c05'
down_revision: str | None = '803cf5974bf3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'notifications_enabled',
                sa.Boolean(),
                server_default='0',
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column('notification_relay_url', sa.String(length=512), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'notification_relay_api_key', sa.String(length=256), nullable=True
            )
        )
        batch_op.drop_column('apns_private_key')
        batch_op.drop_column('apns_bundle_id')
        batch_op.drop_column('apns_key_id')
        batch_op.drop_column('apns_team_id')


def downgrade() -> None:
    """Restore the columns, empty.

    A downgrade cannot bring back a signing key that was dropped, so an install
    that goes back to a pre-relay version has to paste one in again. Recreating
    the columns is still worth doing: the older code reads them, and reading a
    NULL is a supported "push is not configured" state there.
    """
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('apns_team_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('apns_key_id', sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column('apns_bundle_id', sa.String(length=256), nullable=True)
        )
        batch_op.add_column(sa.Column('apns_private_key', sa.Text(), nullable=True))
        batch_op.drop_column('notification_relay_api_key')
        batch_op.drop_column('notification_relay_url')
        batch_op.drop_column('notifications_enabled')
