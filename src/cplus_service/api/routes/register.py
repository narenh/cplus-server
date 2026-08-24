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
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from ...auth.identity import authenticate_plex_token
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


@router.get("/register")
async def register(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
) -> dict[str, str]:
    """Validate the caller's Plex token and prime the cache-only endpoints.

    Nothing in the response body is meaningful to the client beyond the status
    code: 200 means the token is good and the cache mapping is refreshed, 401
    means Seerr rejected it, 502 means Seerr could not be reached.
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

    return {"status": "ok"}
