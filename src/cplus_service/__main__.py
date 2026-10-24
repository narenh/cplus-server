"""Run the service: ``python -m cplus_service``.

Stage 3 owns Docker packaging; this is the plain local entrypoint.

Schema is *not* created automatically — run ``alembic upgrade head`` first, so
the migration history stays the single source of truth for what the database
looks like.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from .api.app import create_app

#: Loggers whose INFO lines are written out. uvicorn's own logging setup gives
#: handlers to its loggers only, so every other logger in this process is left
#: at Python's default of WARNING-and-up through the last-resort handler — an
#: INFO line from this service never reaches the container log. Grab tracking
#: is meant to be timed from that log, so its loggers get a handler of their
#: own. Deliberately just these: switching on INFO for the whole service would
#: change what every existing deployment writes.
VERBOSE_LOGGERS = ("cplus_service.tracking", "cplus_service.qbittorrent")


def configure_logging(level_name: str) -> None:
    """Send the :data:`VERBOSE_LOGGERS` to stderr at ``CPLUS_LOG_LEVEL``."""
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):  # uvicorn's "trace", or a typo
        level = logging.DEBUG if level_name.lower() == "trace" else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in VERBOSE_LOGGERS:
        target = logging.getLogger(name)
        target.addHandler(handler)
        target.setLevel(level)
        target.propagate = False


def main() -> None:
    configure_logging(os.environ.get("CPLUS_LOG_LEVEL", "info"))
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("CPLUS_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("CPLUS_PORT", "8080")),
        log_level=os.environ.get("CPLUS_LOG_LEVEL", "info"),
        # A line per request is noise at this service's scale (the TV polls),
        # and costs a little on every one. CPLUS_ACCESS_LOG=1 brings it back.
        access_log=os.environ.get("CPLUS_ACCESS_LOG", "").lower() in ("1", "true", "yes"),
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
