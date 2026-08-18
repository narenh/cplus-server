"""Async engine and session plumbing.

SQLite via aiosqlite.  ``PRAGMA foreign_keys=ON`` is set per connection because
SQLite defaults it *off*, which would silently ignore every ``ON DELETE`` clause
in :mod:`cplus_service.db.models`.
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


def database_url(path: Path | str | None = None) -> str:
    """Async SQLAlchemy URL for a SQLite file (or ``:memory:`` for tests)."""
    target = Path(path) if path is not None else DEFAULT_DB_PATH
    if str(target) == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{target}"


def create_engine(path: Path | str | None = None, **kwargs: Any) -> AsyncEngine:
    engine = create_async_engine(database_url(path), **kwargs)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
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
