"""The admin app's request-manager endpoints.

A different caller from both tvOS and the browser admin webui: it authenticates
with a Plex token like tvOS does, but — unlike tvOS — always validates live
against Seerr, because these operations (grabbing a specific release directly,
listing download clients, unrestricted search) have no action and no
permission grant of their own to check against the cache. Named ``/manager/*``
after that live check, to keep it visually distinct from tvOS's ``/grab`` and
``/titles/{imdb_id}/actions`` and from the cookie-authenticated ``/admin/*``
webui.

Most of these gate on the ``MANAGE_REQUESTS`` bit. ``/tmdb-token`` and
``/push-devices`` are the exceptions and gate on ``ADMIN`` instead, since
neither has anything to do with managing requests — one hands back a stored
credential, the other decides whose phone gets woken up.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from ...auth.identity import authenticate_plex_token
from ...db.models import ActivityLog, ApnsDevice, EventType
from ...prowlarr.client import ProwlarrError
from ...search.stream import stream_search
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import (
    ConfigDep,
    DbDep,
    PlexTokenDep,
    ProwlarrDep,
    SeerrDep,
    StateDep,
    require_admin,
    require_request_manager,
)
from ..grab_core import execute_grab
from ..schemas import (
    GrabResponse,
    ManagerGrabRequest,
    PushDeviceRegistration,
    PushDeviceResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["manager"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"


@router.post("/grab", response_model=GrabResponse)
async def grab(
    db: DbDep,
    state: StateDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    background: BackgroundTasks,
    body: ManagerGrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release straight to a chosen download client, no action involved.

    Raises no notification: this is an admin doing their own work, and nobody
    wants their phone to tell them what they just did. See
    :func:`~cplus_service.api.grab_core.execute_grab`.
    """
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
        state=state,
        background=background,
    )


@router.get("/search")
async def search(
    db: DbDep,
    config: ConfigDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    imdb_id: str | None = Query(default=None, min_length=1),
    query: str | None = Query(default=None, min_length=1),
    preferred_only: bool = Query(default=False),
) -> StreamingResponse:
    """Unrestricted Prowlarr search for the admin app: by IMDB id or free text.

    Exactly one of ``imdb_id`` or ``query`` must be given. Never scored — there
    is no action here to score against, and picking a release to grab directly
    (``POST /manager/grab``) doesn't need one; every result is returned as-is.

    This is the *only* way to search Prowlarr independent of holding an
    action — regular tvOS users only ever see Prowlarr results through an
    action they hold, at ``GET /titles/{imdb_id}/actions``, which is exactly
    the access control this endpoint would bypass for anyone. Restricted to
    callers who can manage requests and checked against Seerr live, same gate
    as every other ``/manager/*`` endpoint.
    """
    if (imdb_id is None) == (query is None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of imdb_id or query.",
        )

    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={
                "imdb_id": imdb_id,
                "query": query,
                "preferred_only": preferred_only,
                "preferred_indexer_id": config.preferred_indexer_id,
            },
        )
    )

    preferred_indexer_id = config.preferred_indexer_id

    async def body() -> AsyncIterator[str]:
        async for phase in stream_search(
            prowlarr=prowlarr,
            imdb_id=imdb_id,
            query=query,
            preferred_only=preferred_only,
            actions=[],
            preferred_indexer_id=preferred_indexer_id,
        ):
            yield phase.to_ndjson_line()

    return StreamingResponse(
        body(),
        media_type=NDJSON_MEDIA_TYPE,
        # Proxies love to buffer streamed responses; this asks nginx not to.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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


@router.post("/push-devices", response_model=PushDeviceResponse)
async def register_push_device(
    db: DbDep,
    config: ConfigDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    body: PushDeviceRegistration,
) -> PushDeviceResponse:
    """Register this device to receive admin notifications. **Admin only.**

    Gated on ADMIN rather than ``MANAGE_REQUESTS``, and that gate is the whole
    of the access control on notifications: there is no per-device permission
    check at send time, because re-validating every device against Seerr on
    every push would put an outbound call back onto the path that exists to
    avoid one. Passing this endpoint is what makes a device eligible; the
    Notifications tab is where one is taken away again.

    Also gated on the instance's master notification switch, with a 409 when it
    is off. Accepting a registration into an instance that will never send
    anything would leave the app holding a token it believes is live, and would
    quietly accumulate device tokens for a feature nobody switched on. The app
    is expected to look at ``GET /capabilities`` first and not ask at all;
    this is the check that makes that contract true rather than merely
    documented.

    That check runs *before* authenticating, unusually. It leaks nothing —
    ``GET /capabilities`` says the same thing to anyone — and it saves a live
    Seerr round trip on a call whose answer cannot change.

    An upsert, since the app calls this on every launch. A token that comes
    back under a different user — someone signed out and an admin signed in on
    the same device — moves to the new owner rather than accumulating a second
    row, which is also what stops the previous owner from being notified
    through hardware they no longer have.
    """
    if not config.notifications_enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Notifications are switched off for this instance. Check "
            "GET /capabilities before registering.",
        )

    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_admin(auth)

    device = await db.get(ApnsDevice, body.device_token)
    if device is None:
        device = ApnsDevice(device_token=body.device_token, user_id=user.id)
        db.add(device)

    device.user_id = user.id
    device.environment = body.environment
    device.device_name = body.device_name
    device.last_seen_at = datetime.now(UTC)
    await db.flush()

    return PushDeviceResponse(success=True)


@router.delete("/push-devices/{device_token}", response_model=PushDeviceResponse)
async def unregister_push_device(
    db: DbDep, seerr: SeerrDep, plex_token: PlexTokenDep, device_token: str
) -> PushDeviceResponse:
    """Stop sending notifications to this device. **Admin only.**

    For an app signing out. Removing a token that is not registered succeeds:
    the caller asked for it to be gone and it is gone, and reporting 404 would
    only tell them something they cannot act on.

    A caller may only remove their own device. Anything else would let one
    admin silence another's phone from an endpoint that exists for sign-out.
    """
    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_admin(auth)

    device = await db.get(ApnsDevice, device_token)
    if device is not None and device.user_id == user.id:
        await db.delete(device)
        await db.flush()

    return PushDeviceResponse(success=True)


@router.get("/tmdb-token")
async def tmdb_token(
    db: DbDep, config: ConfigDep, seerr: SeerrDep, plex_token: PlexTokenDep
) -> Any:
    """The saved TMDB bearer token, verbatim. **Admin only.**

    This is a deliberate exception to how every other secret in this service
    is handled: the Prowlarr key never leaves the server, and the Seerr admin
    key is never even stored (see ``/seerr/*``). Handing this one back over
    the API trades that same discipline for convenience — it's a low-impact,
    easily rotated key with no access to this service's own data, wanted here
    purely so an admin's own tooling can use it for testing. Gated on the
    ADMIN bit, not ``MANAGE_REQUESTS``, since it has nothing to do with
    managing requests.
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

    require_admin(auth)

    return {"tmdb_bearer_token": config.tmdb_bearer_token}
