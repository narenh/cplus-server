"""Guard against migration drift.

``alembic upgrade head`` on an empty database must produce exactly the schema
``Base.metadata`` describes.  Without this, adding a column to a model in
stage 2 and forgetting the migration would only surface in production.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from cplus_service.db.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_migrations_produce_the_schema_the_models_describe(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    env = {**os.environ, "CPLUS_DB_PATH": str(db_path)}

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        migrated = {
            name: {column["name"] for column in inspector.get_columns(name)}
            for name in inspector.get_table_names()
            if name != "alembic_version"
        }
    finally:
        engine.dispose()

    expected = {
        name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected
