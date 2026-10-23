"""``POST /devices/connection-events`` — how each TV reaches Plex, as it reports it.

The tvOS client records every decision it makes about which of the Plex
server's addresses to use (local, remote or relay), every address it tried
and what happened, and uploads them in batches. The admin console reads them
back to show how friends' and family's TVs connect and what fails; see
:mod:`cplus_service.api.routes.admin.devices`.

Authenticated like every other client route (``X-Plex-Token``, primed by
``/register``, and ``X-Canopy-Profile``). The TV itself is named by the
``X-Plex-Client-Identifier`` the app already sends Plex; its user-entered name
travels in the body as ``deviceName``, because a header cannot carry the
curly apostrophe in "Naren’s Apple TV".

Every event carries a client-made ``id``, and one already stored is dropped,
so a batch resent after a lost answer is harmless. Events are kept for
:data:`RETENTION`, counted from when cplus received them.

The events hold the Plex server's own addresses and never a token. A query
string is dropped from every address anyway, so one can't arrive by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from ...db.models import ClientDevice, ConnectionEvent
from ..deps import CachedUserDep, DbDep, ProfileDep

router = APIRouter(prefix="/devices", tags=["client"])

CLIENT_IDENTIFIER_HEADER = "X-Plex-Client-Identifier"

RETENTION = timedelta(days=90)

#: A TV clock this far ahead of ours is wrong, not early. Its events are
#: stamped with our time instead, or one would stay "current" until its
#: clock caught up.
MAX_CLOCK_SKEW = timedelta(minutes=5)

LAN_NETWORKS = frozenset({"wifi", "ethernet"})
LOCAL_TIERS = frozenset({"localHTTPS", "localHTTP"})


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _clean_uri(uri: str | None) -> str | None:
    """The address without credentials, query or fragment."""
    if not uri:
        return None
    parts = urlsplit(uri)
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))[:512]


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class Endpoint(_CamelModel):
    """An address the TV was or is connected on."""

    uri: str = Field(max_length=512)
    tier: str = Field(max_length=16)
    via_443_retry: bool = Field(default=False, alias="via443Retry")


class Candidate(Endpoint):
    """One address the TV tried, and what happened."""

    outcome: str = Field(max_length=32)
    latency_ms: int | None = Field(default=None, ge=0, alias="latencyMs")
    http_status: int | None = Field(default=None, alias="httpStatus")


class EventIn(_CamelModel):
    """One decision. Tiers, outcomes and reasons are free strings, so an app that
    adds a new one is shown it rather than having its whole batch refused."""

    id: UUID
    at: datetime
    reason: str = Field(max_length=32)
    network: str | None = Field(default=None, max_length=16)
    app_version: str | None = Field(default=None, max_length=32, alias="appVersion")
    server_machine_id: str | None = Field(default=None, max_length=64, alias="serverMachineID")
    previous: Endpoint | None = None
    chosen: Endpoint | None = None
    candidates: list[Candidate] = Field(default_factory=list, max_length=64)


class ConnectionEventsIn(_CamelModel):
    device_name: str | None = Field(default=None, max_length=256, alias="deviceName")
    events: list[EventIn] = Field(max_length=500)


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "uri": _clean_uri(candidate.uri),
        "tier": candidate.tier,
        "via443Retry": candidate.via_443_retry,
        "outcome": candidate.outcome,
        "latencyMs": candidate.latency_ms,
        "httpStatus": candidate.http_status,
    }


def _later(current: datetime | None, stamp: datetime) -> datetime:
    return stamp if current is None or _aware(current) < stamp else _aware(current)


@router.post("/connection-events")
async def post_connection_events(
    body: ConnectionEventsIn,
    db: DbDep,
    user: CachedUserDep,
    profile: ProfileDep,
    client_identifier: Annotated[
        str | None, Header(alias=CLIENT_IDENTIFIER_HEADER, max_length=128)
    ] = None,
) -> dict[str, int]:
    """Store a batch. ``accepted`` counts new events, ``duplicates`` ones already held."""
    if not client_identifier:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Missing {CLIENT_IDENTIFIER_HEADER} header"
        )
    now = datetime.now(UTC)

    device = (
        await db.execute(
            select(ClientDevice).where(
                ClientDevice.user_id == user.id,
                ClientDevice.client_identifier == client_identifier,
            )
        )
    ).scalar_one_or_none()
    if device is None:
        device = ClientDevice(
            user_id=user.id, client_identifier=client_identifier, first_seen_at=now
        )
        db.add(device)
    if body.device_name is not None:
        device.device_name = body.device_name.strip() or None
    device.profile = profile
    device.last_seen_at = now
    await db.flush()

    ids = {str(event.id) for event in body.events}
    held = set(
        (await db.execute(select(ConnectionEvent.id).where(ConnectionEvent.id.in_(ids))))
        .scalars()
        .all()
    )

    accepted = 0
    newest_version: tuple[datetime, str] | None = None
    for event in sorted(body.events, key=lambda e: _aware(e.at)):
        event_id = str(event.id)
        if event_id in held:
            continue
        held.add(event_id)
        accepted += 1

        at = _aware(event.at)
        if at > now + MAX_CLOCK_SKEW:
            at = now
        chosen, previous = event.chosen, event.previous
        db.add(
            ConnectionEvent(
                id=event_id,
                device_id=device.id,
                profile=profile,
                at=at,
                received_at=now,
                reason=event.reason,
                network=event.network,
                app_version=event.app_version,
                server_machine_id=event.server_machine_id,
                previous_uri=_clean_uri(previous.uri) if previous else None,
                previous_tier=previous.tier if previous else None,
                chosen_uri=_clean_uri(chosen.uri) if chosen else None,
                chosen_tier=chosen.tier if chosen else None,
                chosen_via_443=chosen.via_443_retry if chosen else False,
                candidates=[_candidate_json(c) for c in event.candidates],
            )
        )

        if device.current_at is None or _aware(device.current_at) <= at:
            device.current_at = at
            device.current_uri = _clean_uri(chosen.uri) if chosen else None
            device.current_tier = chosen.tier if chosen else None
            device.current_network = event.network
        if event.network in LAN_NETWORKS:
            device.last_lan_at = _later(device.last_lan_at, at)
        if chosen and chosen.tier in LOCAL_TIERS:
            device.last_local_at = _later(device.last_local_at, at)
        if event.app_version and (newest_version is None or newest_version[0] <= at):
            newest_version = (at, event.app_version)

    if newest_version is not None:
        device.app_version = newest_version[1]

    await db.execute(delete(ConnectionEvent).where(ConnectionEvent.received_at < now - RETENTION))
    await db.flush()
    return {"accepted": accepted, "duplicates": len(body.events) - accepted}
