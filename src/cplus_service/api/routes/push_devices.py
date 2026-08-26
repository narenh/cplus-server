"""``/manager/push-devices`` — the app handing its APNs token in and back.

Shares ``/manager``'s prefix and its live-against-Seerr authentication, but not
its permission gate: the rest of ``/manager/*`` needs ``MANAGE_REQUESTS``,
while a device row is the whole of the access control on notifications and so
needs ``ADMIN``. That difference is why this is its own module rather than two
more endpoints in :mod:`cplus_service.api.routes.manager`, whose docstring gets
to keep saying "every endpoint here gates on ``MANAGE_REQUESTS``".

Registration is driven by (OS permission × ``GET /capabilities``), never by
login events — see :mod:`cplus_service.api.routes.capabilities`. The app calls
``POST`` on every launch that finds both conditions true, and ``DELETE`` when
it is signing out or being repointed at another instance.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.identity import authenticate_plex_token
from ...db.models import ApnsDevice, ApnsEnvironment, User
from ...seerr.client import SeerrAuthError, SeerrClient, SeerrError
from ..deps import ConfigDep, DbDep, PlexTokenDep, SeerrDep
from ..schemas import PushDeviceRegistration, PushDeviceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["manager"])


async def _admin_caller(db: AsyncSession, seerr: SeerrClient, plex_token: str) -> User:
    """The caller, having proved to Seerr — live — that they hold ADMIN.

    Live rather than off the token cache because this is the only check the
    feature ever gets: every later push goes out on the strength of the row
    this creates, and never asks Seerr again.

    ADMIN, not ``MANAGE_REQUESTS``: a request manager can approve requests but
    is not the person a notification about the instance is for.
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

    if not auth.user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Notifications are limited to Seerr administrators.",
        )
    return user


@router.post("/push-devices", response_model=PushDeviceResponse)
async def register_push_device(
    db: DbDep,
    config: ConfigDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    body: PushDeviceRegistration,
) -> PushDeviceResponse:
    """Take this device's APNs token, or update the one already held.

    Refused with 409 while notifications are switched off, rather than stored
    against the day they are turned on: an app that got a 200 would believe it
    is registered and stop asking, and this install would meanwhile accumulate
    device tokens for a feature nobody enabled. The detail names
    ``/capabilities`` because that is where the app finds out when the answer
    changes.

    Keyed by the token, so a second call from the same install updates rather
    than duplicates — including a call from a *different* admin, whose sign-in
    moves the device to them. The previous owner no longer has that hardware,
    and would otherwise keep being notified through it.
    """
    user = await _admin_caller(db, seerr, plex_token)

    if not config.notifications_enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Notifications are switched off for this instance. Watch "
            "GET /capabilities for when that changes.",
        )

    device = await db.get(ApnsDevice, body.device_token)
    if device is None:
        device = ApnsDevice(device_token=body.device_token, user_id=user.id)
        db.add(device)

    device.user_id = user.id
    device.environment = ApnsEnvironment(body.environment)
    device.device_name = body.device_name
    device.last_seen_at = datetime.now(UTC)
    await db.flush()

    logger.info(
        "registered push device for user=%s environment=%s", user.id, body.environment
    )
    return PushDeviceResponse(success=True)


@router.delete("/push-devices/{device_token}", response_model=PushDeviceResponse)
async def unregister_push_device(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    device_token: str,
) -> PushDeviceResponse:
    """Hand a device token back — the app signing out, or leaving this instance.

    Never gated on notifications being on. An app repointing at a different
    instance has to be able to clear its registration with the one it is
    leaving, and that is exactly the situation in which the feature may have
    been switched off in the meantime.

    Only the caller's own device: taking away someone else's is the admin
    console's job (``POST /admin/notifications/devices/delete``), which can see
    which device it is removing. A token that is not registered, or belongs to
    another admin, still answers 200 — the caller asked for its own
    registration to be gone, and it is.
    """
    user = await _admin_caller(db, seerr, plex_token)

    device = await db.get(ApnsDevice, device_token)
    if device is not None and device.user_id == user.id:
        await db.delete(device)
        await db.flush()

    return PushDeviceResponse(success=True)
