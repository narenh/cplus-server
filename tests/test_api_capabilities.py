"""``GET /capabilities`` — how the app finds out notifications exist.

The endpoint that makes "the admin enabled notifications six months after
everyone installed the app" work without asking every user to sign out and back
in. Registration is driven by (OS permission × capability flag), never by login
events, so these tests are about the flag being visible at the times login is
not: before sign-in, and on every subsequent launch.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Config

from .conftest import enable_notifications


async def test_a_fresh_install_reports_notifications_off(
    client: httpx.AsyncClient, configured: Config
) -> None:
    response = await client.get("/capabilities")

    assert response.status_code == 200
    assert response.json() == {"notifications": False}


async def test_it_reports_notifications_on_once_the_admin_enables_them(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await enable_notifications(db, configured)

    response = await client.get("/capabilities")

    assert response.json() == {"notifications": True}


async def test_it_needs_no_plex_token(
    client: httpx.AsyncClient, configured: Config
) -> None:
    """The case the endpoint exists for: an app checking before it can sign in.

    Requiring a token would mean the app could not learn whether to prompt for
    notification permission until after login, which is exactly the coupling
    this replaces.
    """
    response = await client.get("/capabilities")

    assert response.status_code == 200


async def test_it_answers_on_an_install_that_has_never_been_configured(
    client: httpx.AsyncClient,
) -> None:
    """No config row yet — first boot, before anyone has opened the admin UI."""
    response = await client.get("/capabilities")

    assert response.status_code == 200
    assert response.json()["notifications"] is False


async def test_it_tracks_the_master_switch_and_not_the_relay_key(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Deliberately not "and a key is set".

    An admin mid-setup would otherwise see the flag flap, and the app has
    nothing useful to do differently in that window. A registration against an
    enabled-but-unconfigured instance is accepted; the Notifications tab is
    where the missing key is reported.
    """
    await enable_notifications(db, configured, api_key=None)

    assert (await client.get("/capabilities")).json() == {"notifications": True}
