"""backfill admin events

Revision ID: 97cb1bac43d5
Revises: c7a29e1f4b83
Create Date: 2026-09-11 00:54:49.988433

``EventType`` grew ``admin`` and ``request`` so a request manager's own work no
longer has to masquerade as a user's grab or search. The event type is a plain
``String(32)``, so there is no schema change — this migration only rewrites the
history already on disk.

Every rewrite is decidable from the row itself:

* ``request_approve`` / ``request_decline`` / ``request_delete`` had a kind and
  are unambiguously the manager's;
* a filed request is ``kind == "request"``;
* an action-free grab is a ``grab`` with no kind and a null ``detail.action_id``.
  A user's action grab always records its action id in the detail, even after
  the action itself is deleted, so the null is reliable;
* the unrestricted manager search is a ``search`` with no kind and no
  ``detail.action_ids`` — that list is written only by the tvOS title search.

Historical rows whose shape predates these markers, if any, are left alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '97cb1bac43d5'
down_revision: str | None = 'c7a29e1f4b83'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Request decisions and deletes were filed as grabs with a kind marker.
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'admin'
        WHERE event_type IN ('search', 'grab')
          AND json_extract(detail, '$.kind') IN
              ('request_approve', 'request_decline', 'request_delete')
        """
    )
    # A user filing a request is its own event, not a grab.
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'request'
        WHERE event_type = 'grab'
          AND json_extract(detail, '$.kind') = 'request'
        """
    )
    # The action-free grab: no kind and no action id.
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'admin',
            detail = json_set(detail, '$.kind', 'grab')
        WHERE event_type = 'grab'
          AND json_extract(detail, '$.kind') IS NULL
          AND json_extract(detail, '$.action_id') IS NULL
        """
    )
    # The unrestricted manager search: no kind and no action_ids.
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'admin',
            detail = json_set(detail, '$.kind', 'search')
        WHERE event_type = 'search'
          AND json_extract(detail, '$.kind') IS NULL
          AND json_extract(detail, '$.action_ids') IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'search',
            detail = json_remove(detail, '$.kind')
        WHERE event_type = 'admin'
          AND json_extract(detail, '$.kind') = 'search'
        """
    )
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'grab',
            detail = json_remove(detail, '$.kind')
        WHERE event_type = 'admin'
          AND json_extract(detail, '$.kind') = 'grab'
        """
    )
    op.execute(
        """
        UPDATE activity_log
        SET event_type = 'grab'
        WHERE event_type IN ('admin', 'request')
          AND json_extract(detail, '$.kind') IN
              ('request', 'request_approve', 'request_decline', 'request_delete')
        """
    )
