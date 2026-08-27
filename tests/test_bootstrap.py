"""First-run seeding.

Both seeds run on every startup and both are conditional, so what is worth
testing is when they *don't* fire as much as when they do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.bootstrap import (
    DEFAULT_PROFILE_NAME,
    ensure_default_quality_profile,
    ensure_request_action,
)
from cplus_service.db.models import QualityProfile
from cplus_service.db.session import create_all, create_engine, create_session_factory
from cplus_service.quality.engine import recommend
from cplus_service.quality.models import FILTER_RULE_TYPES
from cplus_service.quality.models import QualityProfile as ProfileSchema
from cplus_service.release.models import ParsedRelease, Resolution


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(tmp_path / "bootstrap.db")
    await create_all(engine)
    factory = create_session_factory(engine)
    async with factory() as db:
        yield db
    await engine.dispose()


async def test_a_fresh_install_gets_a_starter_quality_profile(
    session: AsyncSession,
) -> None:
    profile = await ensure_default_quality_profile(session)

    assert profile is not None
    assert profile.name == DEFAULT_PROFILE_NAME
    assert profile.rules, "a starter with no rules would pick whatever came first"


async def test_the_starter_profile_filters_nothing(session: AsyncSession) -> None:
    # It is called "All" and has to mean it: an admin who has not decided
    # anything yet must not find candidates silently disappearing.
    profile = await ensure_default_quality_profile(session)
    assert profile is not None

    schema = ProfileSchema(id=profile.id, name=profile.name, rules=profile.rules)
    assert schema.filters == []
    assert schema.preferences

    # Even a release most profiles would drop is still recommendable.
    cam = ParsedRelease(
        title="Movie.2024.CAM.x264-NOBODY",
        guid="cam",
        resolution=Resolution.UNKNOWN,
        is_prerelease=True,
        size_bytes=900 * 1024**2,
    )
    assert recommend([cam], schema) is cam


async def test_the_starter_profile_still_ranks(session: AsyncSession) -> None:
    profile = await ensure_default_quality_profile(session)
    assert profile is not None
    schema = ProfileSchema(id=profile.id, name=profile.name, rules=profile.rules)

    sd = ParsedRelease(title="a", guid="sd", resolution=Resolution.SD_480P, size_bytes=1)
    uhd = ParsedRelease(
        title="b", guid="uhd", resolution=Resolution.UHD_2160P, size_bytes=1
    )

    # Listed second in Prowlarr order, so this is the ranking talking, not luck.
    assert recommend([sd, uhd], schema) is uhd


async def test_the_starter_is_not_seeded_when_the_admin_already_has_profiles(
    session: AsyncSession,
) -> None:
    session.add(QualityProfile(name="Mine", rules=[]))
    await session.flush()

    assert await ensure_default_quality_profile(session) is None

    names = (await session.execute(select(QualityProfile.name))).scalars().all()
    assert list(names) == ["Mine"]


async def test_seeding_the_starter_twice_does_nothing_the_second_time(
    session: AsyncSession,
) -> None:
    first = await ensure_default_quality_profile(session)
    assert first is not None

    assert await ensure_default_quality_profile(session) is None
    count = (await session.execute(select(QualityProfile))).scalars().all()
    assert len(count) == 1


async def test_the_starter_is_an_ordinary_profile_the_admin_may_edit(
    session: AsyncSession,
) -> None:
    # Nothing keys off its name or id — unlike the Request action, which is
    # protected precisely because the client routes on it.
    profile = await ensure_default_quality_profile(session)
    assert profile is not None
    assert not any(
        isinstance(rule, FILTER_RULE_TYPES)
        for rule in ProfileSchema(name=profile.name, rules=profile.rules).rules
    )

    await session.delete(profile)
    await session.flush()
    assert (await session.execute(select(QualityProfile))).scalars().first() is None


async def test_the_request_action_and_the_starter_profile_are_independent(
    session: AsyncSession,
) -> None:
    action = await ensure_request_action(session)
    profile = await ensure_default_quality_profile(session)

    assert action is not None and profile is not None
    # The system action deliberately has no profile: it never touches Prowlarr,
    # so seeding one does not give it something to score against.
    assert action.quality_profile_id is None
