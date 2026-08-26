"""Data model tests.

Everything runs against a real (temporary, on-disk) SQLite database rather than
a mock, because the things worth testing here — the singleton CHECK constraint,
``ON DELETE`` behaviour, JSON round-tripping — are all enforced by SQLite, not
by SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import (
    Action,
    ActivityLog,
    Config,
    EventType,
    Grab,
    Permission,
    QualityProfile,
    User,
)
from cplus_service.db.session import (
    create_all,
    create_engine,
    create_session_factory,
    get_config,
)
from cplus_service.quality.models import QualityProfile as ProfileSchema
from cplus_service.quality.models import default_profile


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(tmp_path / "test.db")
    await create_all(engine)
    factory = create_session_factory(engine)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_user_and_action(session: AsyncSession) -> tuple[User, Action]:
    profile = QualityProfile(name="4K", rules=[])
    session.add(profile)
    await session.flush()

    user = User(seerr_user_id=42, plex_username="someone")
    action = Action(name="Stream Now", download_client_id=5, quality_profile_id=profile.id)
    session.add_all([user, action])
    await session.flush()
    return user, action


# --------------------------------------------------------------------------- #
# Config singleton
# --------------------------------------------------------------------------- #


async def test_get_config_creates_the_row_on_first_access(session: AsyncSession) -> None:
    config = await get_config(session)
    assert config.id == 1
    assert config.preferred_indexer_id is None  # "All indexers"
    assert config.prowlarr_api_key is None


async def test_get_config_is_idempotent(session: AsyncSession) -> None:
    first = await get_config(session)
    first.prowlarr_url = "http://prowlarr.local:9696"
    await session.commit()

    second = await get_config(session)
    assert second.prowlarr_url == "http://prowlarr.local:9696"

    rows = (await session.execute(select(Config))).scalars().all()
    assert len(rows) == 1


async def test_a_second_config_row_is_rejected(session: AsyncSession) -> None:
    await get_config(session)
    await session.commit()

    session.add(Config(id=2, prowlarr_url="http://elsewhere"))
    with pytest.raises(IntegrityError):
        await session.commit()


# --------------------------------------------------------------------------- #
# Users, actions, profiles
# --------------------------------------------------------------------------- #


async def test_seerr_user_id_is_unique(session: AsyncSession) -> None:
    session.add(User(seerr_user_id=1, plex_username="a"))
    await session.commit()

    session.add(User(seerr_user_id=1, plex_username="b"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_action_names_are_unique(session: AsyncSession) -> None:
    profile = QualityProfile(name="p", rules=[])
    session.add(profile)
    await session.flush()

    session.add(Action(name="Stream Now", download_client_id=1, quality_profile_id=profile.id))
    await session.commit()

    session.add(Action(name="Stream Now", download_client_id=2, quality_profile_id=profile.id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_quality_profile_rules_round_trip_as_ordered_json(
    session: AsyncSession,
) -> None:
    schema = default_profile("Balanced")
    stored = QualityProfile(
        name=schema.name, rules=[r.model_dump(mode="json") for r in schema.rules]
    )
    session.add(stored)
    await session.commit()
    session.expunge_all()

    loaded = (
        await session.execute(select(QualityProfile).where(QualityProfile.name == "Balanced"))
    ).scalar_one()

    assert [r["type"] for r in loaded.rules] == [r.type.value for r in schema.rules]
    # And it validates straight back into the pydantic model the engine consumes.
    reparsed = ProfileSchema(id=loaded.id, name=loaded.name, rules=loaded.rules)
    assert reparsed.rules == schema.rules


async def test_a_profile_in_use_by_an_action_cannot_be_deleted(
    session: AsyncSession,
) -> None:
    profile = QualityProfile(name="p", rules=[])
    session.add(profile)
    await session.flush()
    session.add(Action(name="Stream Now", download_client_id=1, quality_profile_id=profile.id))
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text(f"DELETE FROM quality_profiles WHERE id = {profile.id}"))


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #


async def test_permissions_grant_a_subset_of_actions(session: AsyncSession) -> None:
    profile = QualityProfile(name="p", rules=[])
    session.add(profile)
    await session.flush()

    user = User(seerr_user_id=1, plex_username="someone")
    stream = Action(name="Stream Now", download_client_id=1, quality_profile_id=profile.id)
    add4k = Action(name="Add 4K", download_client_id=2, quality_profile_id=profile.id)
    session.add_all([user, stream, add4k])
    await session.flush()

    session.add(Permission(user_id=user.id, action_id=stream.id))
    await session.commit()
    session.expunge_all()

    loaded = (await session.execute(select(User))).scalar_one()
    assert [a.name for a in loaded.actions] == ["Stream Now"]


async def test_the_same_grant_cannot_be_made_twice(session: AsyncSession) -> None:
    user, action = await _seed_user_and_action(session)
    session.add(Permission(user_id=user.id, action_id=action.id))
    await session.commit()

    session.add(Permission(user_id=user.id, action_id=action.id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_a_user_cascades_to_permissions(session: AsyncSession) -> None:
    user, action = await _seed_user_and_action(session)
    session.add(Permission(user_id=user.id, action_id=action.id))
    await session.commit()

    await session.execute(text(f"DELETE FROM users WHERE id = {user.id}"))
    await session.commit()

    remaining = (await session.execute(select(Permission))).scalars().all()
    assert remaining == []


# --------------------------------------------------------------------------- #
# Grabs and activity log
# --------------------------------------------------------------------------- #


async def test_grab_records_the_release_it_sent(session: AsyncSession) -> None:
    user, action = await _seed_user_and_action(session)
    session.add(
        Grab(
            user_id=user.id,
            action_id=action.id,
            release_title="Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
            release_guid="https://indexer.example/details/abc",
            indexer_id=7,
            size_bytes=25_000_000_000,
        )
    )
    await session.commit()
    session.expunge_all()

    grab = (await session.execute(select(Grab))).scalar_one()
    assert grab.size_bytes == 25_000_000_000
    assert grab.created_at is not None


async def test_deleting_an_action_keeps_the_grab_history(session: AsyncSession) -> None:
    user, action = await _seed_user_and_action(session)
    session.add(
        Grab(
            user_id=user.id,
            action_id=action.id,
            release_title="Movie.2024.1080p.WEB-DL-GRP",
            release_guid="guid",
            indexer_id=1,
            size_bytes=1,
        )
    )
    await session.commit()

    await session.execute(text(f"DELETE FROM actions WHERE id = {action.id}"))
    await session.commit()
    session.expunge_all()

    grab = (await session.execute(select(Grab))).scalar_one()
    assert grab.action_id is None
    assert grab.release_title == "Movie.2024.1080p.WEB-DL-GRP"


async def test_activity_log_stores_free_form_detail(session: AsyncSession) -> None:
    user, _ = await _seed_user_and_action(session)
    session.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={"imdb_id": "tt0111161", "result_count": 34},
        )
    )
    await session.commit()
    session.expunge_all()

    entry = (await session.execute(select(ActivityLog))).scalar_one()
    assert entry.event_type == EventType.SEARCH
    assert entry.detail["imdb_id"] == "tt0111161"
    assert entry.detail["result_count"] == 34
