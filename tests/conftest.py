"""Shared API test fixtures.

Tests exercise the real app through ASGI — real routing, real dependencies, real
database — with only the outbound HTTP to Prowlarr and Seerr mocked via respx.
That keeps the auth wiring under test rather than stubbed past.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.api.app import create_app
from cplus_service.db.models import Action, ApnsDevice, Config, Permission, QualityProfile, User
from cplus_service.db.session import create_engine
from cplus_service.notify.relay import RelaySettings

SEERR_URL = "http://seerr.test:5055"
PROWLARR_URL = "http://prowlarr.test:9696"
PROWLARR_API_KEY = "prowlarr-key"
TMDB_BEARER_TOKEN = "tmdb-bearer-token"
PLEX_TOKEN = "plex-token-abc"

RELAY_URL = "https://relay.test"
RELAY_API_KEY = "canopy_testinstance_k3jd7q2mfhx4zt8bwv6nra5cyp"
RELAY_PUSH_URL = f"{RELAY_URL}/v1/push"
RELAY_VERIFY_URL = f"{RELAY_URL}/v1/verify"


def seerr_user_payload(
    *, user_id: int = 42, permissions: int = 32, username: str = "someone"
) -> dict:
    """A Seerr /auth/plex response body. ``permissions=2`` is the ADMIN bit."""
    return {
        "id": user_id,
        "permissions": permissions,
        "plexUsername": username,
        "email": f"{username}@example.com",
    }


@pytest_asyncio.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    engine = create_engine(tmp_path / "api.db")
    application = create_app(engine=engine, create_schema=True)
    async with LifespanManager(application):
        yield application
    await engine.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def db(app: FastAPI) -> AsyncIterator[AsyncSession]:
    """A session against the same database the app uses, for arranging state."""
    async with app.state.cplus.sessionmaker() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture
async def configured(db: AsyncSession) -> Config:
    """Config with both upstreams set and no preferred indexer."""
    config = Config(
        id=1,
        seerr_url=SEERR_URL,
        prowlarr_url=PROWLARR_URL,
        prowlarr_api_key=PROWLARR_API_KEY,
        preferred_indexer_id=None,
        tmdb_bearer_token=TMDB_BEARER_TOKEN,
    )
    db.add(config)
    await db.commit()
    return config


async def make_action(
    db: AsyncSession, name: str, *, download_client_id: int = 5
) -> Action:
    """An ordinary Prowlarr-backed action with a permissive profile."""
    profile = QualityProfile(name=f"{name} profile", rules=[])
    db.add(profile)
    await db.flush()
    action = Action(
        name=name,
        download_client_id=download_client_id,
        quality_profile_id=profile.id,
    )
    db.add(action)
    await db.flush()
    await db.commit()
    return action


async def grant(db: AsyncSession, user: User, action: Action) -> None:
    db.add(Permission(user_id=user.id, action_id=action.id))
    await db.commit()


@pytest.fixture
def plex_headers() -> dict[str, str]:
    return {"X-Plex-Token": PLEX_TOKEN}


# --------------------------------------------------------------------------- #
# Push notifications
# --------------------------------------------------------------------------- #


@pytest.fixture
def relay_settings() -> RelaySettings:
    return RelaySettings(url=RELAY_URL, api_key=RELAY_API_KEY)


async def enable_notifications(
    db: AsyncSession, config: Config, *, api_key: str | None = RELAY_API_KEY
) -> Config:
    """Turn notifications on and point them at the mocked relay.

    ``api_key=None`` is the half-configured state — switched on, nowhere to
    send — which several tests need to tell apart from switched off.
    """
    config.notifications_enabled = True
    config.notification_relay_url = RELAY_URL
    config.notification_relay_api_key = api_key
    db.add(config)
    await db.commit()
    return config


async def register_device(
    db: AsyncSession,
    user: User,
    *,
    device_token: str = "aa" * 16,
    environment: str = "production",
    device_name: str = "Test device",
) -> ApnsDevice:
    device = ApnsDevice(
        device_token=device_token,
        user_id=user.id,
        environment=environment,
        device_name=device_name,
    )
    db.add(device)
    await db.commit()
    return device
