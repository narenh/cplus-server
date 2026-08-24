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

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, Engine, create_engine, inspect

from cplus_service.db.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def migrated_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """An engine over a database built purely by ``alembic upgrade head``."""
    db_path = tmp_path_factory.mktemp("migrations") / "migrated.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "CPLUS_DB_PATH": str(db_path)},
        capture_output=True,
        text=True,
        check=False,
    )
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
