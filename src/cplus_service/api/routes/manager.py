"""The admin app's request-manager endpoints.

A different caller from both tvOS and the browser admin webui: it authenticates
with a Plex token like tvOS does, but — unlike tvOS — always validates live
against Seerr and gates on the ``MANAGE_REQUESTS`` bit, because these
operations (grabbing a specific release directly, listing download clients)
have no action and no permission grant of their own to check against the
cache. Named ``/manager/*`` after that gate, to keep it visually distinct from
tvOS's ``/grab`` and from the cookie-authenticated ``/admin/*`` webui.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from ...auth.identity import authenticate_plex_token
from ...prowlarr.client import ProwlarrError
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, ProwlarrDep, SeerrDep, require_request_manager
from ..grab_core import execute_grab
from ..schemas import GrabResponse, ManagerGrabRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["manager"])


@router.post("/grab", response_model=GrabResponse)
async def grab(
    db: DbDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    body: ManagerGrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release straight to a chosen download client, no action involved."""
    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            exc.detail or "Seerr rejected this Plex token",
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    return await execute_grab(
        db,
        prowlarr,
        user=user,
        action=None,
        download_client_id=body.download_client_id,
        body=body,
    )


@router.get("/download-clients")
async def list_download_clients(
    db: DbDep, prowlarr: ProwlarrDep, seerr: SeerrDep, plex_token: PlexTokenDep
) -> Any:
    """Prowlarr's download clients, for the admin app's grab picker.

    The web UI has its own session-gated copy of this; the admin app
    authenticates with a Plex token instead, so it needs one of its own. Same
    gate as the action-free grab above — if you cannot grab directly, knowing
    the client list is no use to you.
    """
    try:
        _, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    try:
        clients = await prowlarr.list_download_clients()
    except ProwlarrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Prowlarr: {exc}"
        ) from exc

    return {
        "download_clients": [
            {"id": c.id, "name": c.name, "enable": c.enable, "protocol": c.protocol}
            for c in clients
        ]
    }
