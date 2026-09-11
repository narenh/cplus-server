"""Async engine and session plumbing.

SQLite via aiosqlite.  Three pragmas are set per connection, because SQLite's
defaults are wrong for a service that serves concurrent requests out of one
file:

``foreign_keys=ON``
    Defaults *off*, which would silently ignore every ``ON DELETE`` clause in
    :mod:`cplus_service.db.models`.

``journal_mode=WAL``
    Defaults to the rollback journal, under which a single open read
    transaction blocks every writer and a single open write transaction blocks
    every reader.  Requests here are not uniformly short — a streamed search
    outlives an ordinary one by a minute — and every authenticated request
    writes, so under the default one slow request is enough to fail the rest.
    WAL lets readers and writers coexist; only writer-against-writer still
    serialises.  It is a property of the database file, not the connection, so
    setting it on every connect is a no-op after the first.

``busy_timeout``
    Python's ``sqlite3`` defaults to 5 seconds, after which a contended write
    raises ``database is locked`` — which reaches the client as a bare HTTP
    500.  Waiting longer is strictly better than failing: the lock it is
    waiting on is held by a request that is itself about to finish.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import CONFIG_SINGLETON_ID, Base, Config

DEFAULT_DB_PATH = Path(os.environ.get("CPLUS_DB_PATH", "cplus.db"))

#: How long a statement waits for a lock another connection holds before giving
#: up with ``database is locked``.  Generous on purpose: the writes here are
#: single rows, so anything this waits on is a request that is already ending.
BUSY_TIMEOUT_MS = 30_000


def database_url(path: Path | str | None = None) -> str:
    """Async SQLAlchemy URL for a SQLite file (or ``:memory:`` for tests)."""
    target = Path(path) if path is not None else DEFAULT_DB_PATH
    if str(target) == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{target}"


def create_engine(path: Path | str | None = None, **kwargs: Any) -> AsyncEngine:
    engine = create_async_engine(database_url(path), **kwargs)

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # Silently a no-op for ``:memory:``, which has no journal to switch.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema directly from the models.

    Convenience for tests and first-run bootstrap; production deployments should
    run ``alembic upgrade head`` so the migration history stays authoritative.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_config(session: AsyncSession) -> Config:
    """Read the singleton config row, creating it empty on first access."""
    config = await session.get(Config, CONFIG_SINGLETON_ID)
    if config is None:
        config = Config(id=CONFIG_SINGLETON_ID)
        session.add(config)
        await session.flush()
    return config


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on exception."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "AsyncSession",
    "create_all",
    "create_engine",
    "create_session_factory",
    "database_url",
    "get_config",
    "session_scope",
]
