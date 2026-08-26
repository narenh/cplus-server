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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.api.app import create_app
from cplus_service.db.models import Action, ApnsDevice, Config, Permission, QualityProfile, User
from cplus_service.db.session import create_engine
from cplus_service.notify.apns import ApnsSettings

SEERR_URL = "http://seerr.test:5055"
PROWLARR_URL = "http://prowlarr.test:9696"
PROWLARR_API_KEY = "prowlarr-key"
TMDB_BEARER_TOKEN = "tmdb-bearer-token"
PLEX_TOKEN = "plex-token-abc"

APNS_TEAM_ID = "TEAM123456"
APNS_KEY_ID = "KEY1234567"
APNS_BUNDLE_ID = "com.example.cplus"
APNS_PRODUCTION = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.sandbox.push.apple.com"


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


@pytest.fixture(scope="session")
def apns_key_pem() -> str:
    """A throwaway P-256 key in the shape Apple's ``.p8`` files come in.

    Generated rather than checked in, so the repository never carries something
    shaped like a signing key. Session-scoped because keygen is the slowest
    thing in these tests by a wide margin.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture
def apns_settings(apns_key_pem: str) -> ApnsSettings:
    return ApnsSettings(
        team_id=APNS_TEAM_ID,
        key_id=APNS_KEY_ID,
        bundle_id=APNS_BUNDLE_ID,
        private_key_pem=apns_key_pem,
    )


async def configure_apns(db: AsyncSession, config: Config, key_pem: str) -> Config:
    """Fill in the four APNs fields, so ``ApnsSettings.from_config`` is satisfied."""
    config.apns_team_id = APNS_TEAM_ID
    config.apns_key_id = APNS_KEY_ID
    config.apns_bundle_id = APNS_BUNDLE_ID
    config.apns_private_key = key_pem
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
