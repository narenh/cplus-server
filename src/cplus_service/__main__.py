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
        # This service is meant to run behind a reverse proxy (Coolify's, a
        # bare Traefik, nginx, ...) which terminates TLS and forwards plain
        # HTTP. Trusting the forwarded headers is what lets the app know the
        # original request was HTTPS, which in turn is what marks the admin
        # session cookie Secure.
        #
        # Trusting every peer is safe *because* of that topology: the container
        # publishes to the proxy, not to the internet. If you ever expose this
        # port directly, set CPLUS_FORWARDED_ALLOW_IPS to the proxy's address.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("CPLUS_FORWARDED_ALLOW_IPS", "*"),
    )


if __name__ == "__main__":
    main()
