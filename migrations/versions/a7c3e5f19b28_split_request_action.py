"""split the built-in Request action into Request Movies and Request TV

One action meant one permission for filing any request, so an admin could not
let someone ask for a series without also letting them ask for films. Each
built-in action now carries the media type it files for in ``request_type``.

The existing action becomes the films one, and keeps its id, its grants, its
rank and the words on its button. If it still has its seeded name it is renamed
"Request Movies", because two actions can no longer share "Request" as a name;
its button title is pinned to "Request" so the television reads the same as
before. The TV action is created as a copy of it, and **everyone who held
Request is granted both**, so nobody gains or loses anything on upgrade.

A fresh database has no built-in action yet — startup seeds them, see
``bootstrap.ensure_request_actions`` — so there is nothing to split.

Revision ID: a7c3e5f19b28
Revises: f4b7d2e9a1c3
Create Date: 2026-10-22 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c3e5f19b28'
down_revision: str | None = 'f4b7d2e9a1c3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _free_name(conn: sa.Connection, wanted: str) -> str:
    """``wanted``, or ``wanted`` with a number after it if an action has it."""
    taken = {row[0] for row in conn.execute(sa.text("SELECT name FROM actions"))}
    candidate, suffix = wanted, 2
    while candidate in taken:
        candidate, suffix = f"{wanted} {suffix}", suffix + 1
    return candidate


def upgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_type', sa.String(length=8), nullable=True))
        batch_op.create_index(batch_op.f('ix_actions_request_type'), ['request_type'], unique=True)

    conn = op.get_bind()
    existing = conn.execute(
        sa.text(
            "SELECT id, name, display_title, sort_order, icon, confirm_body"
            " FROM actions WHERE is_system = 1 ORDER BY id LIMIT 1"
        )
    ).first()

    if existing is not None:
        movie_id, name, display_title, sort_order, icon, confirm_body = existing
        button = display_title or name

        movie_name = name
        if name == 'Request':
            movie_name = _free_name(conn, 'Request Movies')
        conn.execute(
            sa.text(
                "UPDATE actions SET request_type = 'movie', name = :name,"
                " display_title = :title WHERE id = :id"
            ),
            {"name": movie_name, "title": button, "id": movie_id},
        )

        tv_name = _free_name(conn, 'Request TV' if name == 'Request' else f"{name} (TV)")
        conn.execute(
            sa.text(
                "INSERT INTO actions (name, display_title, sort_order, icon, confirm_body,"
                " download_client_id, quality_profile_id, is_system, request_type)"
                " VALUES (:name, :title, :sort_order, :icon, :confirm_body,"
                " NULL, NULL, 1, 'tv')"
            ),
            {
                "name": tv_name,
                "title": button,
                "sort_order": sort_order,
                "icon": icon,
                "confirm_body": confirm_body,
            },
        )
        tv_id = conn.execute(
            sa.text("SELECT id FROM actions WHERE request_type = 'tv'")
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO permissions (user_id, action_id)"
                " SELECT user_id, :tv_id FROM permissions WHERE action_id = :movie_id"
            ),
            {"tv_id": tv_id, "movie_id": movie_id},
        )

    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_action_request_type_only_on_system',
            "(is_system = 1 AND request_type IN ('movie', 'tv'))"
            " OR (is_system = 0 AND request_type IS NULL)",
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Explicitly, not by cascade: SQLite enforces foreign keys only when asked.
    conn.execute(
        sa.text(
            "DELETE FROM permissions WHERE action_id IN"
            " (SELECT id FROM actions WHERE request_type = 'tv')"
        )
    )
    conn.execute(sa.text("DELETE FROM actions WHERE request_type = 'tv'"))
    conn.execute(
        sa.text(
            "UPDATE actions SET name = 'Request' WHERE request_type = 'movie'"
            " AND name = 'Request Movies'"
            " AND NOT EXISTS (SELECT 1 FROM actions WHERE name = 'Request')"
        )
    )

    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_constraint('ck_action_request_type_only_on_system', type_='check')
        batch_op.drop_index(batch_op.f('ix_actions_request_type'))
        batch_op.drop_column('request_type')
