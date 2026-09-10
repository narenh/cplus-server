"""``GET /defaults`` — the fresh-install seed for Libraries & Home.

A companion to ``GET /register``, called once a caller's Plex token is known
good: a device signing in for the first time can seed its local Library and
Home state from whatever the admin has already configured on the
:doc:`Libraries & Home admin tab <cplus_service.api.routes.admin.libraries>`,
rather than starting from the app's own hardcoded fallbacks. ``GET /register``
can also fold this same payload into its own response — see
:func:`defaults_payload` and ``register.register``'s ``first_run`` parameter
— which is the path an actual first run should prefer, since it needs no
second round trip.

Every shelf-shaped value here — ``default_home_shelves``, and
``default_carousel``'s own ``carousel``/``top_shelf`` — is stored, and
returned, in exactly the shape CanopyPlus's own ``HomeShelfDataModel``
decodes, plus one field with no counterpart there yet: ``modifiedAt``,
stamped on every edit (see ``admin.libraries._apply_shelf_update``) so a
future sync layer has something to diff against from day one.
``default_libraries`` is CanopyPlus's own ``MediaLibrary`` shape, unchanged.
See ``cplus_service.db.models.Config`` for where each column's own
docstring says so, and ``cplus_service.api.routes.admin.libraries`` and
``.home_sources`` for what actually writes them.

Cache-only auth, same as ``GET /titles/{imdb_id}/actions`` and
``GET /search``: no outbound Plex or Seerr call.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import Response

from ...bootstrap import upnext_shelf
from ...db.session import get_config
from ..deps import CachedUserDep, DbDep

router = APIRouter(tags=["client"])


async def defaults_payload(db: DbDep) -> dict[str, object]:
    """The admin's current Library and Home configuration, as a plain dict.

    Shared by ``GET /defaults`` and ``GET /register``'s ``first_run``
    bundling, so the two never drift apart. ``default_carousel``'s
    ``carousel`` and ``top_shelf`` fall back to the same "Continue Watching"
    default the admin webui itself seeds at startup
    (``bootstrap.ensure_default_carousel``, ``.ensure_default_top_shelf``) if
    the database somehow still has neither on record — this should never
    500 just because that seeding hasn't run yet. ``default_home_shelves``
    has the same fallback for the same reason, though in practice
    ``ensure_default_home_shelf`` and the admin UI's own "keep at least one"
    rule mean it is never actually empty.
    """
    config = await get_config(db)
    return {
        "default_libraries": config.default_libraries,
        "default_home_shelves": config.home_shelves or [upnext_shelf()],
        "default_carousel": {
            "enabled": config.home_carousel_enabled,
            "include_on_deck": config.home_carousel_include_on_deck,
            "carousel": config.home_carousel or upnext_shelf(),
            "top_shelf": config.home_top_shelf or upnext_shelf(),
        },
    }


@router.get("/defaults")
async def defaults(db: DbDep, user: CachedUserDep) -> Response:
    """The admin's current Library and Home configuration.

    Indented for now so it is easy to read by eye while this is still being
    built out; a later pass will switch this to FastAPI's own compact
    default rather than hand-rolling the response.
    """
    payload = await defaults_payload(db)
    return Response(content=json.dumps(payload, indent=2), media_type="application/json")
