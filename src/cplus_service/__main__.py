"""Run the service: ``python -m cplus_service``.

Stage 3 owns Docker packaging; this is the plain local entrypoint.

Schema is *not* created automatically — run ``alembic upgrade head`` first, so
the migration history stays the single source of truth for what the database
looks like.
"""

from __future__ import annotations

import os

import uvicorn

from .api.app import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("CPLUS_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("CPLUS_PORT", "8080")),
        log_level=os.environ.get("CPLUS_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
