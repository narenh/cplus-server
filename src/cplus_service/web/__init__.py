"""Server-rendered admin webui: Jinja2 templates, HTMX, no build step.

This is a single-admin internal tool, not a product UI, so there is deliberately
no SPA framework, no bundler and no npm. HTMX is vendored under ``static/`` so a
deployed container needs no CDN access.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

BYTES_PER_GB = 1024**3


def format_size(value: Any) -> str:
    """Bytes as GB, or an em dash when the indexer never reported a size."""
    if not isinstance(value, int) or value <= 0:
        return "—"
    return f"{value / BYTES_PER_GB:.2f} GB"


def format_when(value: Any) -> str:
    """A UTC timestamp rendered for a human, tolerating SQLite's naive datetimes."""
    if not isinstance(value, datetime):
        return "—"
    stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


@cache
def static_url(name: str) -> str:
    """``/static/<name>`` with a content hash on the end.

    **The origin's own ``Cache-Control: no-cache`` is not enough.** A CDN in
    front of this service (Cloudflare, in the deployment this was written for)
    will happily replace that header with a browser TTL of its own — four hours,
    by default, on anything ending in ``.css`` or ``.js``. The HTML is dynamic
    and is not cached, so a deploy lands new markup against the previous
    stylesheet, and the admin gets a page whose layout rules no longer exist.

    A hash of the file's bytes makes a changed file a different URL, which no
    cache anywhere can serve from a copy of the old one. Computed once per
    process: these files are baked into the image and cannot change under a
    running container.

    A name that does not exist is returned unversioned rather than raising —
    a missing asset is already visible as a 404, and a template should not be
    the thing that takes the whole page down.
    """
    path = STATIC_DIR / name
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={digest}"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["size"] = format_size
templates.env.filters["when"] = format_when
templates.env.globals["static_url"] = static_url

__all__ = [
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "format_size",
    "format_when",
    "static_url",
    "templates",
]
