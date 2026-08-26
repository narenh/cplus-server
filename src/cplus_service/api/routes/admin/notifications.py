"""The Notifications tab: what to be told about, and where to send it.

Three separate things share the page because they fail in the same place —
an admin who is not getting notifications needs to see, in one screen, whether
the type is switched off, whether push is configured, and whether their device
ever registered. Splitting them across tabs would only make that harder.

The switches write through immediately, one round trip per toggle, in the same
style as the permissions grid. The APNs credentials are an ordinary form with a
Save button, because four fields that are only valid together should not be
half-applied while someone is still typing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import ApnsDevice, User
from ....db.session import get_config
from ....notify import prefs
from ....notify.apns import ApnsConfigError, ApnsSettings, validate_private_key
from ....notify.messages import MediaSummary, user_requested
from ....notify.service import DispatchReport, eligible_devices, send_to_devices
from ....notify.types import (
    NOTIFICATION_TYPES,
    NOTIFICATION_TYPES_BY_VALUE,
    NotificationType,
    NotificationTypeInfo,
)
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["admin"])

#: What the "send a test" button sends. Deliberately a real, ordinary-looking
#: notification rather than one that says TEST: the point of the button is to
#: show an admin exactly what will land on their lock screen.
TEST_MEDIA = MediaSummary(title="The End of Oak Street", year=2026)
TEST_USERNAME = "Jane Dietrich"


async def _devices_with_owners(
    db: AsyncSession,
) -> list[tuple[ApnsDevice, User | None]]:
    """Registered devices, newest last, each with the admin who registered it."""
    rows = await db.execute(
        select(ApnsDevice, User)
        .outerjoin(User, User.id == ApnsDevice.user_id)
        .order_by(ApnsDevice.created_at)
    )
    return [(device, user) for device, user in rows.all()]


@router.get("", response_class=HTMLResponse)
async def notifications_page(
    request: Request, db: DbDep, admin: AdminPageDep
) -> Response:
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "types": NOTIFICATION_TYPES,
            "enabled": await prefs.current(db),
            "config": config,
            "apns_configured": ApnsSettings.from_config(config) is not None,
            "devices": await _devices_with_owners(db),
            "admin": admin,
            "title": "Notifications",
            "nav": "notifications",
        },
    )


@router.post("/types/{notification_type}", response_class=HTMLResponse)
async def toggle_type(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    notification_type: str,
    enabled: str = Form(default=""),
) -> Response:
    """Switch one notification type on or off. Idempotent."""
    try:
        parsed = NotificationType(notification_type)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No such notification type"
        ) from exc

    is_enabled = await prefs.set_enabled(db, parsed, enabled == "on")

    return templates.TemplateResponse(
        request,
        "partials/notification_toggle.html",
        {"info": _info_for(parsed), "is_enabled": is_enabled},
    )


def _info_for(notification_type: NotificationType) -> NotificationTypeInfo:
    """The catalogue entry for a type. Every enum member has one by construction."""
    return NOTIFICATION_TYPES_BY_VALUE[notification_type.value]


@router.post("/apns", response_class=HTMLResponse)
async def save_apns(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    apns_team_id: str = Form(default=""),
    apns_key_id: str = Form(default=""),
    apns_bundle_id: str = Form(default=""),
    apns_private_key: str = Form(default=""),
) -> Response:
    """Store the APNs credentials.

    The key follows the same rule as the Prowlarr API key: an empty field means
    "leave it alone", so the stored key is never rendered back into the page
    and cannot be blanked by someone saving a change to the team id.

    A key that will not parse is rejected here rather than at the first push.
    Otherwise the page would report success and the failure would surface later
    as notifications silently not arriving, which is the hardest possible way
    to notice a pasted-wrong file.
    """
    config = await get_config(db)
    config.apns_team_id = apns_team_id.strip() or None
    config.apns_key_id = apns_key_id.strip() or None
    config.apns_bundle_id = apns_bundle_id.strip() or None

    if apns_private_key.strip():
        candidate = apns_private_key.strip()
        try:
            validate_private_key(candidate)
        except ApnsConfigError as exc:
            return templates.TemplateResponse(
                request, "partials/verify.html", {"ok": False, "message": str(exc)}
            )
        config.apns_private_key = candidate

    await db.flush()

    settings = ApnsSettings.from_config(config)
    if settings is None:
        message = (
            "Saved. Push stays off until the key, key id, team id and bundle id "
            "are all set."
        )
    else:
        message = "Saved. Push is configured."

    return templates.TemplateResponse(
        request, "partials/verify.html", {"ok": True, "message": message}
    )


@router.post("/test", response_class=HTMLResponse)
async def send_test(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    """Send a sample notification to every registered device.

    Every device *including the caller's own* — the opposite of the rule on the
    emitting path, and correct here: an admin pressing this wants to see it
    arrive on the phone in their hand. It also ignores the type switches, since
    the question being asked is whether delivery works at all.
    """
    config = await get_config(db)
    settings = ApnsSettings.from_config(config)
    if settings is None:
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {
                "ok": False,
                "message": "Add the key, key id, team id and bundle id first, then save.",
            },
        )

    devices = await eligible_devices(db)
    if not devices:
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {
                "ok": False,
                "message": (
                    "No devices are registered. Sign in on the app once with an "
                    "admin account and it will register itself."
                ),
            },
        )

    report = await send_to_devices(
        db,
        notification=user_requested(TEST_MEDIA, username=TEST_USERNAME),
        devices=devices,
        settings=settings,
        http=state.apns_http,
        tokens=state.apns_tokens,
    )

    return templates.TemplateResponse(
        request, "partials/verify.html", _test_result(report)
    )


def _test_result(report: DispatchReport) -> dict[str, object]:
    """Turn a dispatch report into one sentence an admin can act on."""
    if report.skipped_reason:
        return {"ok": False, "message": report.skipped_reason}

    parts = [f"Sent to {report.delivered} device{'' if report.delivered == 1 else 's'}."]
    if report.unregistered:
        plural = "" if report.unregistered == 1 else "s"
        parts.append(
            f"Removed {report.unregistered} device{plural} Apple no longer recognises."
        )
    if report.failed:
        parts.append(f"{report.failed} failed — the server log has Apple's reason.")

    return {
        "ok": report.delivered > 0 and not report.failed,
        "message": " ".join(parts),
    }


@router.post("/devices/delete")
async def delete_device(
    db: DbDep, admin: AdminPageDep, device_token: str = Form(...)
) -> Response:
    """Remove a device from the admin console.

    Unlike the app's own sign-out at ``DELETE /manager/push-devices/{token}``,
    this is not restricted to the caller's own devices: taking away a lost or
    handed-on device belonging to someone else is exactly what an admin console
    is for.

    The token travels in the form body rather than the path, which keeps a
    64-character device address out of the browser history and out of every
    reverse proxy's access log. The app's own endpoint keeps it in the path —
    that caller is sending back a token it already holds, over an API where the
    resource shape is worth more than the log hygiene.
    """
    device = await db.get(ApnsDevice, device_token)
    if device is not None:
        await db.delete(device)
        await db.flush()

    return RedirectResponse("/admin/notifications", status_code=status.HTTP_303_SEE_OTHER)
