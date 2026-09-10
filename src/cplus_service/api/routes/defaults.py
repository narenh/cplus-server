"""``GET /defaults`` — the fresh-install seed for Libraries & Home.

A companion to ``GET /register``, called once a caller's Plex token is known
good: a device signing in for the first time can seed its local Library and
Home state from whatever the admin has already configured on the
:doc:`Libraries & Home admin tab <cplus_service.api.routes.admin.libraries>`,
rather than starting from the app's own hardcoded fallbacks.

Every value here is stored — and returned — in exactly the shape
CanopyPlus's own Codable structs decode: ``MediaLibrary`` for each entry of
``default_libraries``, ``HomeShelfDataModel`` for ``default_top_shelf``,
``default_carousel`` and each entry of ``default_shelves``. See
``cplus_service.db.models.Config`` for where each column's own docstring
says so, and ``cplus_service.api.routes.admin.libraries`` and
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


@router.get("/defaults")
async def defaults(db: DbDep, user: CachedUserDep) -> Response:
    """The admin's current Library and Home configuration.

    ``default_top_shelf`` and ``default_carousel`` fall back to the same
    "Continue Watching" default the admin webui itself seeds at startup
    (``bootstrap.ensure_default_carousel``, ``.ensure_default_top_shelf``) if
    the database somehow still has neither on record — this should never
    500 just because that seeding hasn't run yet. ``default_shelves`` has
    the same fallback for the same reason, though in practice
    ``ensure_default_home_shelf`` and the admin UI's own "keep at least one"
    rule mean it is never actually empty.

    Indented for now so it is easy to read by eye while this is still being
    built out; a later pass will switch this to FastAPI's own compact
    default rather than hand-rolling the response.
    """
    config = await get_config(db)
    payload = {
        "default_libraries": config.default_libraries,
        "default_top_shelf": config.home_top_shelf or upnext_shelf(),
        "default_carousel_enabled": config.home_carousel_enabled,
        "default_carousel": config.home_carousel or upnext_shelf(),
        "default_shelves": config.home_shelves or [upnext_shelf()],
    }
    return Response(content=json.dumps(payload, indent=2), media_type="application/json")
