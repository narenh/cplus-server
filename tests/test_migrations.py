"""Guard against migration drift.

``alembic upgrade head`` on an empty database must produce exactly the schema
``Base.metadata`` describes.  Without this, adding a column to a model in
stage 2 and forgetting the migration would only surface in production.

Columns alone are not enough.  ``actions`` leans on a CHECK constraint to keep
a non-system action from existing without a download client — ``/grab`` treats
that as guaranteed — and the hot query paths lean on indexes.  Either one could
be added to a model and forgotten in a migration without changing any column
name, so the comparison covers all three.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, Engine, create_engine, inspect

from cplus_service.db.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The revision the admin/request backfill starts from — the head before it.
PRE_BACKFILL_REVISION = "c7a29e1f4b83"


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "CPLUS_DB_PATH": str(db_path)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def migrated_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """An engine over a database built purely by ``alembic upgrade head``."""
    db_path = tmp_path_factory.mktemp("migrations") / "migrated.db"
    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        yield engine
    finally:
        engine.dispose()


def _tables(engine: Engine) -> list[str]:
    return [
        name
        for name in inspect(engine).get_table_names()
        if name != "alembic_version"
    ]


def test_migrations_produce_the_columns_the_models_describe(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    migrated = {
        name: {column["name"] for column in inspector.get_columns(name)}
        for name in _tables(migrated_engine)
    }
    expected = {
        name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected


def test_migrations_produce_the_check_constraints_the_models_describe(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    migrated = {
        name: {
            constraint["name"]
            for constraint in inspector.get_check_constraints(name)
            if constraint.get("name")
        }
        for name in _tables(migrated_engine)
    }
    expected = {
        name: {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected

    # The one ``/grab`` depends on: it is why a non-system action can be assumed
    # to carry a download client.
    assert "ck_action_targets_required_unless_system" in migrated["actions"]


def test_migrations_produce_the_indexes_the_models_describe(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    migrated = {
        name: {
            index["name"] for index in inspector.get_indexes(name) if index.get("name")
        }
        for name in _tables(migrated_engine)
    }
    expected = {
        name: {index.name for index in table.indexes if index.name}
        for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected


def test_backfill_reclassifies_historical_events(tmp_path: Path) -> None:
    """The data migration rewrites only the rows it can actually identify.

    Each case is a shape the old code wrote: a user action grab, the admin's
    action-free grab, a filed request, a manager decision, a tvOS search and the
    manager's unrestricted search. The event type is a plain string, so this is
    the only thing standing between the old rows and the new vocabulary.
    """
    db_path = tmp_path / "backfill.db"
    result = _run_alembic(db_path, "upgrade", PRE_BACKFILL_REVISION)
    assert result.returncode == 0, result.stderr

    # (initial event_type, detail, expected event_type, expected kind)
    rows: list[tuple[str, dict, str, str | None]] = [
        ("grab", {"action_id": 3, "release_title": "A"}, "grab", None),
        ("grab", {"action_id": None, "release_guid": "g"}, "admin", "grab"),
        ("grab", {"kind": "request", "tmdb_id": 1}, "request", "request"),
        (
            "grab",
            {"kind": "request_approve", "seerr_request_id": 7},
            "admin",
            "request_approve",
        ),
        (
            "grab",
            {"kind": "request_delete", "seerr_request_id": 8},
            "admin",
            "request_delete",
        ),
        ("search", {"imdb_id": "tt1", "action_ids": [1]}, "search", None),
        ("search", {"imdb_id": "tt2"}, "admin", "search"),
    ]

    connection = sqlite3.connect(db_path)
    for event_type, detail, _, _ in rows:
        connection.execute(
            "INSERT INTO activity_log (event_type, detail, created_at)"
            " VALUES (?, ?, CURRENT_TIMESTAMP)",
            (event_type, json.dumps(detail)),
        )
    connection.commit()
    connection.close()

    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    connection = sqlite3.connect(db_path)
    stored = connection.execute(
        "SELECT event_type, detail FROM activity_log ORDER BY id"
    ).fetchall()
    connection.close()

    migrated = [(event_type, json.loads(detail).get("kind")) for event_type, detail in stored]
    assert migrated == [(expected_type, kind) for *_, expected_type, kind in rows]


def test_via_manager_is_backfilled_from_the_activity_log(tmp_path: Path) -> None:
    """Which historical grabs were the admin's is only knowable from the log.

    The grabs table itself cannot say: both an admin grab and a grab whose
    action was deleted leave a null ``action_id``. The activity log recorded
    each successful grab's action id, and only the action-free one wrote null
    there — so that is where the answer comes from.
    """
    db_path = tmp_path / "via_manager.db"
    result = _run_alembic(db_path, "upgrade", PRE_BACKFILL_REVISION)
    assert result.returncode == 0, result.stderr

    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO users (id, seerr_user_id, plex_username) VALUES (1, 42, 'admin')"
    )
    connection.executemany(
        "INSERT INTO grabs (user_id, action_id, release_title, release_guid,"
        " indexer_id, created_at) VALUES (1, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
        [
            (None, "Admin.Grab-GRP", "guid-admin"),
            (None, "Orphaned.Grab-GRP", "guid-orphan"),
            (3, "Action.Grab-GRP", "guid-action"),
        ],
    )
    connection.executemany(
        "INSERT INTO activity_log (user_id, event_type, detail, created_at)"
        " VALUES (1, 'grab', ?, CURRENT_TIMESTAMP)",
        [
            (json.dumps({"action_id": None, "release_guid": "guid-admin", "success": True}),),
            (json.dumps({"action_id": 3, "release_guid": "guid-orphan", "success": True}),),
            (json.dumps({"action_id": 3, "release_guid": "guid-action", "success": True}),),
        ],
    )
    connection.commit()
    connection.close()

    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    connection = sqlite3.connect(db_path)
    stored = dict(
        connection.execute("SELECT release_guid, via_manager FROM grabs").fetchall()
    )
    connection.close()

    # The orphan's action was deleted after the fact — still not an admin grab.
    assert stored == {"guid-admin": 1, "guid-orphan": 0, "guid-action": 0}
