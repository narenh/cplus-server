"""The Devices page: which screens this install serves, and what to call them.

A subpage of Grabs rather than a tab of its own, because it exists for one
reason: the Grabs and Activity tables print a device per row, and a device is a
UUID until somebody names it. The link sits next to those tables and the page
is nothing but the nicknaming.

There is no "add device" anything. A row appears the first time a request
arrives carrying ``X-Plex-Client-Identifier`` — see
:func:`cplus_service.api.deps.get_calling_device` — so the list is always
exactly the set of installs that have actually talked to this service, and an
admin's only job here is to put a name to one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import PlexDevice
from ....web import templates
from ...deps import DbDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["admin"])

#: Matches ``PlexDevice.nickname``. Enforced here as well as in the input's
#: ``maxlength`` — a browser attribute is a convenience, not a constraint.
MAX_NICKNAME_LENGTH = 64


async def device_index(db: AsyncSession) -> dict[str, PlexDevice]:
    """Every known device, keyed by identifier, for a page that renders labels.

    Shared with the Grabs and Activity pages, which look each row's device up in
    this rather than joining: the identifier on a ``grabs`` row is a
    denormalised copy with no foreign key behind it (an admin may remove a
    device the history still mentions), so a lookup that tolerates a miss is the
    only correct read. Small by nature — one row per screen in a household.
    """
    rows = await db.execute(select(PlexDevice))
    return {device.client_identifier: device for device in rows.scalars().all()}


async def _devices(db: AsyncSession) -> list[PlexDevice]:
    """Known devices, most recently seen first — the ones still in use on top."""
    rows = await db.execute(select(PlexDevice).order_by(PlexDevice.last_seen_at.desc()))
    return list(rows.scalars().all())


@router.get("", response_class=HTMLResponse)
async def devices_page(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "devices": await _devices(db),
            "admin": admin,
            "title": "Devices",
            # Highlights Grabs in the nav — this is a subpage of it, not a tab —
            # and the breadcrumb is the way back.
            "nav": "grabs",
            "breadcrumb": [("/admin/grabs", "Grabs")],
        },
    )


@router.post("/nickname", response_class=HTMLResponse)
async def set_nickname(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    client_identifier: str = Form(...),
    nickname: str = Form(default=""),
) -> Response:
    """Name one device, or clear its name.

    An empty field means "clear it", and the row falls back to whatever the
    client calls itself and then to the bare identifier. That is the only way
    back: there is no separate delete-the-nickname control, because an empty
    text box already says it unambiguously.

    The identifier travels in the body rather than the path for the same reason
    an APNs token does on the Notifications tab: it is an opaque
    client-supplied string, and one that has no business being URL-shaped.

    Answers with just this row. A nickname changes nothing else on the page —
    not the ordering, not the empty state — so swapping the whole list would
    only cost the admin their place in it. The swapped-in row carries the new
    value as its own ``data-original``, which is what puts its Save button away
    again; there is no "saved" text, the same as renaming a library.
    """
    device = await db.get(PlexDevice, client_identifier)
    if device is None:
        # The one way to get here is a device removed in another tab. Say so,
        # rather than resurrecting a row from a stale form.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That device is no longer listed. Reload the page.",
        )

    cleaned = " ".join(nickname.split())
    if len(cleaned) > MAX_NICKNAME_LENGTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A nickname can be at most {MAX_NICKNAME_LENGTH} characters.",
        )

    device.nickname = cleaned or None
    await db.flush()

    return templates.TemplateResponse(
        request, "partials/device_row.html", {"device": device}
    )


@router.post("/delete", response_class=HTMLResponse)
async def delete_device(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    client_identifier: str = Form(...),
) -> Response:
    """Forget a device — a screen that was sold, reinstalled or replaced.

    Two consequences worth knowing, both stated on the page: the nickname is
    gone, so every grab that device made goes back to showing a raw identifier;
    and if the device is still in use it reappears, unnamed, on its very next
    request. Nothing in the history is deleted — there is deliberately no
    foreign key from ``grabs`` to here, so this removes a label and nothing
    else.

    Answers with the whole list, unlike a nickname save: losing the last row
    hands the page over to its empty state.
    """
    device = await db.get(PlexDevice, client_identifier)
    if device is not None:
        await db.delete(device)
        await db.flush()

    return templates.TemplateResponse(
        request, "partials/devices_list.html", {"devices": await _devices(db)}
    )
