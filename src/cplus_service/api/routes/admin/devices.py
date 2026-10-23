"""The TVs tab: how each tvOS client reaches Plex, from what it reports.

One row per TV with its current connection and two warnings; a TV's own page
is the timeline of its decisions. Both read what
:mod:`cplus_service.api.routes.devices` stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ....db.models import ClientDevice, ConnectionEvent, User
from ....web import templates
from ...deps import DbDep
from .deps import AdminPageDep

router = APIRouter(tags=["admin"])

PAGE_SIZE = 200

#: Best first, as the client ranks them.
TIERS = ("localHTTPS", "localHTTP", "remoteHTTPS", "remoteHTTP", "relay")
TIER_LABELS = {
    "localHTTPS": "Local HTTPS",
    "localHTTP": "Local HTTP",
    "remoteHTTPS": "Remote HTTPS",
    "remoteHTTP": "Remote HTTP",
    "relay": "Relay",
}
#: Failures worth an admin's eye: they usually mean the network, not Plex.
LOUD_FAILURES = frozenset({"dnsFailure", "tlsError", "timeout"})
OUTCOME_LABELS = {
    "answered": "answered",
    "wrongServer": "wrong server",
    "timeout": "timeout",
    "dnsFailure": "DNS failure",
    "tlsError": "TLS error",
    "httpStatus": "HTTP error",
    "cancelled": "cancelled",
    "other": "failed",
}


def tier_kind(tier: str | None) -> str:
    """The badge colour for a tier: local, remote, relay, or none."""
    if tier is None:
        return "none"
    if tier.startswith("local"):
        return "local"
    if tier.startswith("remote"):
        return "remote"
    return "relay" if tier == "relay" else "other"


def outcome_kind(outcome: str) -> str:
    if outcome == "answered":
        return "ok"
    if outcome in LOUD_FAILURES:
        return "loud"
    return "muted" if outcome == "cancelled" else "quiet"


@dataclass
class DeviceRow:
    device: ClientDevice
    username: str
    never_local: bool
    on_relay: bool


def _row(device: ClientDevice, username: str) -> DeviceRow:
    return DeviceRow(
        device=device,
        username=username,
        never_local=device.last_lan_at is not None and device.last_local_at is None,
        on_relay=device.current_tier == "relay",
    )


def _grouped(candidates: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Candidates by tier, best tier first; tiers the app made up since go last."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault(str(candidate.get("tier") or "?"), []).append(candidate)
    rank = {tier: i for i, tier in enumerate(TIERS)}
    return sorted(groups.items(), key=lambda item: rank.get(item[0], len(TIERS)))


def _context() -> dict[str, Any]:
    return {
        "tier_labels": TIER_LABELS,
        "tier_kind": tier_kind,
        "outcome_kind": outcome_kind,
        "outcome_labels": OUTCOME_LABELS,
    }


@router.get("/devices", response_class=HTMLResponse)
async def list_devices(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    result = await db.execute(
        select(ClientDevice, User.plex_username)
        .join(User, User.id == ClientDevice.user_id)
        .order_by(ClientDevice.last_seen_at.desc())
    )
    rows = [_row(device, username) for device, username in result.all()]
    return templates.TemplateResponse(
        request,
        "devices.html",
        {**_context(), "rows": rows, "admin": admin, "title": "TVs", "nav": "devices"},
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_detail(
    request: Request, device_id: int, db: DbDep, admin: AdminPageDep
) -> Response:
    found = (
        await db.execute(
            select(ClientDevice, User.plex_username)
            .join(User, User.id == ClientDevice.user_id)
            .where(ClientDevice.id == device_id)
        )
    ).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such TV")
    device, username = found
    events = (
        await db.execute(
            select(ConnectionEvent)
            .where(ConnectionEvent.device_id == device.id)
            .order_by(ConnectionEvent.at.desc(), ConnectionEvent.received_at.desc())
            .limit(PAGE_SIZE)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "device_detail.html",
        {
            **_context(),
            "row": _row(device, username),
            "events": [(event, _grouped(event.candidates or [])) for event in events],
            "page_size": PAGE_SIZE,
            "admin": admin,
            "title": device.device_name or "Unnamed TV",
            "breadcrumb": [("/admin/devices", "TVs")],
            "nav": "devices",
        },
    )
