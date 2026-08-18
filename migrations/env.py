"""Alembic environment.

The database URL comes from ``CPLUS_DB_PATH`` (same env var the app uses) rather
than being hardcoded in ``alembic.ini``, so ``alembic upgrade head`` and the
running service always agree on which file they are talking about.

Migrations run against a synchronous SQLite driver even though the app is async
— Alembic has no reason to be async, and it keeps ``env.py`` boring.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from cplus_service.db.models import Base
from cplus_service.db.session import DEFAULT_DB_PATH

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", f"sqlite:///{DEFAULT_DB_PATH}")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, which future migrations will need.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
