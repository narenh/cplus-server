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
from ....notify.relay import DEFAULT_RELAY_URL, RelaySettings, verify
from ....notify.service import (
    DISABLED_REASON,
    UNCONFIGURED_REASON,
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


async def _page_context(db: AsyncSession, config: Config) -> dict[str, object]:
    """Everything the settings block renders from.

    Shared by the full page and the partial the master switch swaps in, so the
    two cannot drift into showing different things about the same state.
    """
    return {
        "types": NOTIFICATION_TYPES,
        "enabled": await prefs.current(db),
        "config": config,
        "relay_url": config.notification_relay_url or DEFAULT_RELAY_URL,
        "default_relay_url": DEFAULT_RELAY_URL,
        "relay_configured": RelaySettings.from_config(config) is not None,
        "devices": await _devices_with_owners(db),
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
    request: Request, db: DbDep, admin: AdminPageDep, enabled: str = Form(default="")
) -> Response:
    """Flip the master switch, and re-render everything it governs.

    The response is the settings block, not the whole page: the checkbox itself
    lives outside the swapped region, so it keeps the state the admin just put
    it in rather than being replaced underneath their cursor.

    Turning it off deliberately **does not** delete registered devices. They are
    inert while it is off — nothing is sent and nothing new may register — and
    keeping them means an admin who toggles this while investigating something
    does not silently cost every admin their registration, with no way to get it
    back but asking each of them to relaunch the app.
    """
    config = await get_config(db)
    config.notifications_enabled = enabled == "on"
    await db.flush()

    return templates.TemplateResponse(
        request,
        "partials/notification_settings.html",
        await _page_context(db, config),
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


@router.post("/relay", response_class=HTMLResponse)
async def save_relay(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    relay_url: str = Form(default=""),
    relay_api_key: str = Form(default=""),
) -> Response:
    """Store the relay URL and API key.

    The key follows the same rule as the Prowlarr API key: an empty field means
    "leave it alone", so the stored key is never rendered back into the page and
    cannot be blanked by someone saving a change to the URL.

    A blank URL resets to the default rather than storing nothing. An install
    with a key and no relay to send it to is not a state anyone means to be in,
    and the default is what almost everyone wants.
    """
    config = await get_config(db)
    config.notification_relay_url = relay_url.strip() or DEFAULT_RELAY_URL

    if relay_api_key.strip():
        config.notification_relay_api_key = relay_api_key.strip()

    await db.flush()

    if RelaySettings.from_config(config) is None:
        message = "Saved. Nothing will be sent until an API key is set."
    else:
        message = "Saved. Use “Check the relay” to confirm the key works."

    return templates.TemplateResponse(
        request, "partials/verify.html", {"ok": True, "message": message}
    )


@router.post("/relay/check", response_class=HTMLResponse)
async def check_relay(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    """Ask the relay whether this install's key works.

    Separate from "send a test notification" because they answer different
    questions and fail for different reasons: this one needs no registered
    device and tells an admin whether the *credential* is good, which is the
    thing they just typed in and the thing they can act on immediately.
    """
    config = await get_config(db)
    settings = RelaySettings.from_config(config)
    if settings is None:
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {"ok": False, "message": _unconfigured_message(config)},
        )

    result = await verify(settings, client=state.relay_http)
    return templates.TemplateResponse(
        request, "partials/verify.html", {"ok": result.ok, "message": result.message}
    )


def _unconfigured_message(config: Config) -> str:
    """Which of the two "not set up" states this install is in."""
    if not config.notifications_enabled:
        return DISABLED_REASON
    return UNCONFIGURED_REASON


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
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {"ok": False, "message": _unconfigured_message(config)},
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
        parts.append(f"{report.failed} failed — the server log has the reason.")

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
