"""Server-rendered admin webui: Jinja2 templates, HTMX, no build step.

This is a single-admin internal tool, not a product UI, so there is deliberately
no SPA framework, no bundler and no npm. HTMX is vendored under ``static/`` so a
deployed container needs no CDN access.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["size"] = format_size
templates.env.filters["when"] = format_when

__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "format_size", "format_when", "templates"]
