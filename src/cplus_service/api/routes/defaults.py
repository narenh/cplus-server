"""The fresh-install seed for Libraries & Home, bundled into ``GET /register``.

There is no standalone route here — this used to be its own ``GET /defaults``
endpoint, called once a caller's Plex token was already known good. It was
folded entirely into ``GET /register`` instead (see :func:`defaults_payload`
and ``register.register``'s ``first_run`` parameter) rather than kept
alongside it: an actual first run now needs exactly one round trip, not two.

Two keys, and they are seeds for very different reasons:

``default_libraries``
    A **one-shot seed, and only that.** The admin's ordered, curated set, in
    CanopyPlus's own ``MediaLibrary`` shape. It is not synced and never pushed
    back: a client applies it only when it has no library selection of its own,
    and an existing install is never repointed by it. There is deliberately no
    cap here — the admin may name as many as they like, because the client is
    what filters the list down to the ones *this* user can actually see and
    then takes the first few. An admin listing eight libraries and a user with
    access to four is the ordinary case, not an error.

``home``
    A whole Home document, in exactly the shape ``GET /home`` returns and
    ``PUT /home`` accepts — see :mod:`cplus_service.home`. Bundled purely to
    save a round trip on the one launch where the client has nothing local to
    show yet; unlike libraries, this one *is* synced from then on, in both
    directions, and the bundle is not the authority on it. It resolves through
    :func:`~cplus_service.home.effective_home`, so a user an admin has already
    given a Home of their own gets theirs and not the global default.
"""

from __future__ import annotations

from ...db.session import get_config
from ...home import document, effective_home
from ..deps import DbDep


async def defaults_payload(db: DbDep, user_id: int) -> dict[str, object]:
    """This user's Library seed and current Home, as a plain dict.

    The one caller is ``register.register``'s ``first_run`` bundling.
    """
    config = await get_config(db)
    return {
        "default_libraries": config.default_libraries,
        "home": document(await effective_home(db, user_id)),
    }
