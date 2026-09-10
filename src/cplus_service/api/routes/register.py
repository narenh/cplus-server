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
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ...auth.identity import authenticate_plex_token
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep
from .defaults import defaults_payload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


@router.get("/register")
async def register(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    first_run: bool | None = Query(default=None),
) -> Response:
    """Validate the caller's Plex token and prime the cache-only endpoints.

    Beyond the status code, nothing in the response body was ever
    meaningful to the client — 200 means the token is good and the cache
    mapping is refreshed, 401 means Seerr rejected it, 502 means Seerr could
    not be reached — and that is still true for ``status`` here. Everything
    else in the body is the ``first_run`` bundle described above, additive
    and safe for a client that has never heard of it to ignore.
    """
    try:
        await authenticate_plex_token(db, seerr, plex_token)
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

    body: dict[str, object] = {"status": "ok"}
    if first_run is True:
        body.update(await defaults_payload(db))
    return Response(content=json.dumps(body, indent=2), media_type="application/json")
