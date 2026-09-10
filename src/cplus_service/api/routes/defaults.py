"""The fresh-install seed for Libraries & Home, bundled into ``GET /register``.

There is no standalone route here any more — this used to be its own
``GET /defaults`` endpoint, called once a caller's Plex token was already
known good. It was folded entirely into ``GET /register`` instead (see
:func:`defaults_payload` and ``register.register``'s ``first_run``
parameter) rather than kept alongside it: an actual first run now needs
exactly one round trip, not two, and a caller with no reason to fetch this
never pays for a second endpoint that always agreed with the first anyway.

Every shelf-shaped value here — ``default_home_shelves``, and
``default_carousel``'s own ``carousel``/``top_shelf`` — is stored, and
returned, in exactly the shape CanopyPlus's own ``HomeShelfDataModel``
decodes: no extra fields. ``default_libraries`` is CanopyPlus's own
``MediaLibrary`` shape, unchanged. See ``cplus_service.db.models.Config``
for where each column's own docstring says so, and
``cplus_service.api.routes.admin.libraries`` and ``.home_sources`` for what
actually writes them.

``Config.home_modified_at`` — one stamp for the whole Home document,
matching CanopyPlus's own ``HomeSettings.modifiedAt`` — is not part of this
bundle yet: this is still the fresh-install seed, not the sync endpoint a
future ``HomeSettings`` sync will need, so nothing here reads or returns it
today.
"""

from __future__ import annotations

from ...bootstrap import upnext_shelf
from ...db.session import get_config
from ..deps import DbDep


async def defaults_payload(db: DbDep) -> dict[str, object]:
    """The admin's current Library and Home configuration, as a plain dict.

    The one caller is ``register.register``'s ``first_run`` bundling.
    ``default_carousel``'s ``carousel`` and ``top_shelf`` fall back to the
    same "Continue Watching" default the admin webui itself seeds at
    startup (``bootstrap.ensure_default_carousel``, ``.ensure_default_top_shelf``)
    if the database somehow still has neither on record — this should
    never fail just because that seeding hasn't run yet. ``default_home_shelves``
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
