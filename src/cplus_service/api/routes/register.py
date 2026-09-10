"""``GET /register`` — the tvOS auth checkpoint.

This is the only tvOS-facing route that validates against Seerr for real. It is
called on app launch and whenever the user reconnects to an instance in
settings, and its side effect — writing the Plex-token → user mapping into the
cache — is what makes the cache-only ``/titles/{imdb_id}/actions``, ``/search``
and ``/grab`` possible.

Actions only make sense in the context of a title — a button's label and its
recommended release both depend on which movie is on screen — so this endpoint
does not describe them at all. It answers exactly one question: is this Plex
token good, and if so, the cache is now primed. ``GET /titles/{imdb_id}/actions``
is where the caller finds out what it can actually do.

There is no session token: either this returns 200 or it 401s, and the
client's only recovery is to call it again.

**``first_run``** folds the Libraries & Home seed (see
``.defaults.defaults_payload``) straight into this response — there is no
separate ``GET /defaults`` endpoint to call first — so an actual first run
needs exactly one round trip. The bundle is included only when
``first_run`` is present and ``true``; absent (today's client, which has
never heard of this parameter) or ``false`` both mean "ordinary launch, no
bundle" — a client passes ``first_run=true`` exactly once, on the call
that has nothing local to seed from yet.

**``plex_server`` rides along on every call, first run or not.** It names the
Plex Media Server this instance is configured against, and it is here rather
than on the unauthenticated ``/capabilities`` because nothing needs it before
a caller is known good. A client that discovered this instance's URL out of a
Plex collection summary checks that identifier against the server it is
actually signed in to before binding: same identifier, same library ids, and
``default_libraries`` means what it says. ``null`` is a real answer — an admin
who has not yet connected this instance to Plex — and says only "this cannot
be verified", not "this is the wrong server".
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ...auth.identity import authenticate_plex_token
from ...db.models import Config
from ...db.session import get_config
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep
from .defaults import defaults_payload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


def plex_server_identity(config: Config) -> dict[str, str] | None:
    """Which Plex Media Server this instance is bound to, or ``None``.

    ``None`` when the admin has never connected one — the client's own
    binding check has nothing to compare against and has to decide what to do
    about that. Only the identity is reported, never
    ``plex_server_base_url``: that is the address *this service* reaches the
    server on, which is frequently not one the client could use and is none of
    its business either way.
    """
    if not config.plex_server_client_identifier:
        return None
    return {
        "client_identifier": config.plex_server_client_identifier,
        "name": config.plex_server_name or "",
    }


@router.get("/register")
async def register(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    first_run: bool | None = Query(default=None),
) -> Response:
    """Validate the caller's Plex token and prime the cache-only endpoints.

    The status code carries the whole of the auth answer, and always did:
    200 means the token is good and the cache mapping is refreshed, 401 means
    Seerr rejected it, 502 means Seerr could not be reached. ``status`` in the
    body says nothing more than the code already did.

    The rest is additive and safe for a client that has never heard of it to
    ignore: ``plex_server`` on every call, and the ``first_run`` bundle on the
    one call that asks for it. Both are described above.
    """
    try:
        user, _auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        # Seerr being unreachable is an upstream fault, not a bad token; saying
        # 401 here would make the client throw away a perfectly good token.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    config = await get_config(db)
    body: dict[str, object] = {
        "status": "ok",
        "plex_server": plex_server_identity(config),
    }
    if first_run is True:
        body.update(await defaults_payload(db, user.id))
    return Response(content=json.dumps(body, indent=2), media_type="application/json")
