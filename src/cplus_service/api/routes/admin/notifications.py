"""The Notifications tab: whether to notify at all, about what, and where to send it.

The page is built around one decision and three consequences of it.

The decision is the **master switch**, and it is the only setting in the whole
admin UI that is off by default. Turning it on routes notification text through
a relay this admin does not run, in plaintext — see :mod:`cplus_service.notify.relay`
— and that is not something to arrive at by pasting a key into a form that was
already half-configured. So the page says what it means, in the sentence next to
the checkbox, and everything else stays hidden until it is on.

The three consequences share the page because they fail in the same place: an
admin who is not getting notifications needs to see, in one screen, whether the
type is switched off, whether the relay key works, and whether their device ever
registered.

The switches and the master toggle write through immediately, one round trip
each, in the same style as the permissions grid. The relay settings are an
ordinary form with a Save button, because a URL and a key that are only
meaningful together should not be half-applied while someone is still typing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import ApnsDevice, Config, User
from ....db.session import get_config
from ....notify import prefs
from ....notify.messages import MediaSummary, user_requested
from ....notify.relay import EnrollmentError, RelaySettings, enrol, relay_base_url
from ....notify.service import (
    DISABLED_REASON,
    DispatchReport,
    eligible_devices,
    send_to_devices,
)
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
TEST_USERNAME = "Robin Example"


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


async def _page_context(
    db: AsyncSession, config: Config, *, error: str | None = None
) -> dict[str, object]:
    """Everything the panel renders from.

    Shared by the full page and the partial the master switch swaps in, so the
    two cannot drift into showing different things about the same state.

    ``error`` is how a failed enrollment reaches the page. It is passed rather
    than raised because the swap has to render *something*, and the something
    has to include the checkbox in its true (still off) position.
    """
    return {
        "types": NOTIFICATION_TYPES,
        "enabled": await prefs.current(db),
        "config": config,
        "relay_host": relay_base_url().removeprefix("https://").removeprefix("http://"),
        "relay_connected": RelaySettings.from_config(config) is not None,
        "devices": await _devices_with_owners(db),
        "error": error,
    }


@router.get("", response_class=HTMLResponse)
async def notifications_page(
    request: Request, db: DbDep, admin: AdminPageDep
) -> Response:
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            **await _page_context(db, config),
            "admin": admin,
            "title": "Notifications",
            "nav": "notifications",
        },
    )


@router.post("/enabled", response_class=HTMLResponse)
async def toggle_enabled(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    enabled: str = Form(default=""),
) -> Response:
    """Flip the master switch, enrolling with the relay the first time it goes on.

    **This is the entire setup.** No URL, no API key, no Save, no Check — an
    admin who wants notifications ticks one box and is done. The credential
    exists, but it is the relay's business and this server's, not theirs: it
    identifies this install for rate-limiting and cannot reach anyone's devices,
    so asking a human to carry it was friction dressed up as security.

    Enrollment happens once. A second enable reuses the stored key rather than
    burning a fresh identity every time someone toggles the switch while
    looking at something else.

    **A failed enrollment leaves the switch off**, and says why. Turning it on
    anyway would produce an install that reports itself capable, accepts device
    registrations, and silently sends nothing — which is precisely the failure
    the old settings form was so good at producing.
    """
    config = await get_config(db)
    wants_on = enabled == "on"
    error: str | None = None

    if wants_on and not (config.notification_relay_api_key or "").strip():
        try:
            enrollment = await enrol(client=state.relay_http)
        except EnrollmentError as exc:
            logger.warning("could not enrol with the notification relay: %s", exc)
            error = str(exc)
            wants_on = False
        else:
            config.notification_relay_instance_id = enrollment.instance_id
            config.notification_relay_api_key = enrollment.api_key
            logger.info(
                "enrolled with the notification relay as %s", enrollment.instance_id
            )
            if not enrollment.ready:
                error = (
                    "Connected, but the relay has no Apple signing key installed "
                    "yet, so nothing can be delivered. Nothing is wrong on this "
                    "end — the relay's operator has to finish setting it up."
                )

    config.notifications_enabled = wants_on
    await db.flush()

    return templates.TemplateResponse(
        request,
        "partials/notification_panel.html",
        await _page_context(db, config, error=error),
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


@router.post("/reconnect", response_class=HTMLResponse)
async def reconnect(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    """Discard this install's relay identity and enrol again.

    The recovery path for the one failure an admin cannot otherwise get out of:
    a key the relay no longer accepts, because it was revoked or because the
    relay's signing secret was rotated. Nothing can be repaired in place — the
    relay stores no keys, so there is nothing to look up — and re-enrolling is
    both the fix and the only fix.

    Cheap enough to offer as a button: a new identity costs an enrollment and
    loses nothing, since the old one was only ever a name in a rate-limit
    bucket. Registered devices are untouched.
    """
    config = await get_config(db)

    try:
        enrollment = await enrol(client=state.relay_http)
    except EnrollmentError as exc:
        return templates.TemplateResponse(
            request, "partials/verify.html", {"ok": False, "message": str(exc)}
        )

    config.notification_relay_instance_id = enrollment.instance_id
    config.notification_relay_api_key = enrollment.api_key
    await db.flush()

    logger.info("re-enrolled with the notification relay as %s", enrollment.instance_id)

    message = f"Reconnected as {enrollment.instance_id}."
    if not enrollment.ready:
        message += (
            " The relay still has no Apple signing key of its own, so nothing "
            "can be delivered yet."
        )

    return templates.TemplateResponse(
        request, "partials/verify.html", {"ok": enrollment.ready, "message": message}
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

    It does *not* ignore the master switch. That one is not a preference about
    which notifications to send; it is consent to use the relay, and a button
    that routes text through a third party after an admin declined would be
    a straightforward betrayal of the checkbox.
    """
    config = await get_config(db)
    settings = RelaySettings.from_config(config)
    if settings is None:
        # Two states left, and they need different instructions: switched off,
        # or switched on with an enrollment that did not stick.
        message = (
            DISABLED_REASON
            if not config.notifications_enabled
            else "Not connected to the relay. Press “Reconnect” and try again."
        )
        return templates.TemplateResponse(
            request, "partials/verify.html", {"ok": False, "message": message}
        )

    devices = await eligible_devices(db)
    if not devices:
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {
                "ok": False,
                "message": (
                    "No devices are registered. Open the app on a device signed "
                    "in as an admin — it registers itself once notifications are "
                    "switched on here."
                ),
            },
        )

    report = await send_to_devices(
        db,
        notification=user_requested(TEST_MEDIA, username=TEST_USERNAME),
        devices=devices,
        settings=settings,
        http=state.relay_http,
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
        reason = report.failure_reason
        parts.append(
            f"{report.failed} failed: {reason}."
            if reason
            else f"{report.failed} failed — the server log has the reason."
        )

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
