"""grabs.via_manager

A null ``action_id`` on a ``grabs`` row carries two unrelated meanings: the
admin app's action-free grab never had an action, and an ordinary grab loses
its reference when the action is deleted (``ON DELETE SET NULL``, so the
history survives). The grabs page cannot tell them apart from the row, so it
called both "deleted action" — labelling every grab an admin had ever made as
history pointing at something gone.

``97cb1bac43d5`` fixed the same ambiguity for the activity log, where the event
type now says which is which. This is the grabs table's own answer, and it is a
column rather than a lookup so it stays true no matter what happens to the
actions table afterwards.

Backfilled from the activity log, which is the only record of which historical
grabs had no action: a successful grab event with a null ``detail.action_id``
is an admin grab, and after ``97cb1bac43d5`` it is typed ``admin`` as well.

Revision ID: a1d6f30b24e9
Revises: 97cb1bac43d5
Create Date: 2026-09-11 02:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1d6f30b24e9'
down_revision: str | None = '97cb1bac43d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _detail(raw: str | None) -> dict:
    """One row's ``detail``, or an empty mapping if it is unreadable.

    Defensive because this column has always been free-form JSON that nothing
    queried into: an unparseable row leaves its grab at the default rather than
    failing the upgrade.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def upgrade() -> None:
    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'via_manager',
                sa.Boolean(),
                nullable=False,
                server_default='0',
            )
        )

    connection = op.get_bind()

    # Read in Python rather than with json_extract so the backfill does not
    # depend on the JSON1 extension being compiled into whichever SQLite the
    # container ships.
    manager_grabs: set[tuple[int, str]] = set()
    for row in connection.execute(
        sa.text(
            "SELECT user_id, detail FROM activity_log"
            " WHERE event_type IN ('grab', 'admin')"
        )
    ):
        detail = _detail(row.detail)
        guid = detail.get('release_guid')
        if (
            detail.get('success') is True
            and detail.get('action_id') is None
            and detail.get('kind') in (None, 'grab')
            and guid
        ):
            manager_grabs.add((row.user_id, guid))

    for user_id, guid in manager_grabs:
        connection.execute(
            sa.text(
                "UPDATE grabs SET via_manager = 1"
                " WHERE action_id IS NULL AND user_id = :user_id"
                " AND release_guid = :guid"
            ),
            {'user_id': user_id, 'guid': guid},
        )


def downgrade() -> None:
    with op.batch_alter_table('grabs', schema=None) as batch_op:
        batch_op.drop_column('via_manager')
