"""The admin UI's prose, kept in ``web/copy/*.toml`` so it can be edited without touching templates.

One file per page (``config.toml``, ``actions.toml``…). A string's key is
``<file stem>.<table path>``, e.g. ``[prowlarr_key]`` / ``hint = '''…'''`` in
``config.toml`` is ``config.prowlarr_key.hint``. Templates pull it in with
``{{ t('config.prowlarr_key.hint') }}``.

Each string is itself a tiny Jinja template rendered with the calling
template's variables, and its output is trusted HTML. So ``<code>``, ``{{ var }}``
and ``{% if %}`` all work inside a string. Use TOML's ``'''`` literal blocks so
quotes and backslashes need no escaping.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, pass_context
from jinja2.runtime import Context
from markupsafe import Markup

COPY_DIR = Path(__file__).parent / "copy"


def _flatten(prefix: str, table: dict[str, Any], out: dict[str, str]) -> None:
    for key, value in table.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            _flatten(path, value, out)
        else:
            out[path] = str(value).strip()


@cache
def load_copy() -> dict[str, str]:
    strings: dict[str, str] = {}
    for path in sorted(COPY_DIR.glob("*.toml")):
        _flatten(path.stem, tomllib.loads(path.read_text(encoding="utf-8")), strings)
    return strings


def install(env: Environment) -> None:
    """Register ``t()`` on a Jinja environment."""
    strings = load_copy()
    # Strict so a variable that doesn't reach the string (a loop variable, say,
    # which the caller's context doesn't carry) fails loudly instead of printing
    # nothing. Pass those explicitly: t('key', book=book).
    strict = env.overlay(undefined=StrictUndefined)

    @pass_context
    def t(ctx: Context, key: str, **extra: Any) -> Markup:
        try:
            source = strings[key]
        except KeyError:
            raise KeyError(f"no copy string {key!r} in {COPY_DIR}") from None
        return Markup(strict.from_string(source).render({**ctx.get_all(), **extra}))

    env.globals["t"] = t


_plain = Environment(autoescape=False, undefined=StrictUndefined)


def text(key: str, **values: Any) -> str:
    """A copy string for Python code (notifications, API errors), rendered as plain text.

    Same files and Jinja syntax as ``t()`` in templates, but no HTML escaping.
    """
    return _plain.from_string(load_copy()[key]).render(values)
