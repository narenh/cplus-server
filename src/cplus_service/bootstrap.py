"""First-run seeding.

The built-in Request action must always exist, so it is seeded idempotently on
startup rather than by a data migration — that way an existing deployment gains
it on upgrade without anyone running anything.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Action

logger = logging.getLogger(__name__)

#: The system action's name is part of the client contract: tvOS routes a button
#: to ``POST /request`` instead of ``POST /grab`` by matching on it. Because a
#: system action cannot be renamed or deleted, that match is stable — but stage
#: 3's admin UI must refuse to create or rename any other action to this name.
REQUEST_ACTION_NAME = "Request"


async def get_request_action(session: AsyncSession) -> Action | None:
    """The built-in Request action, identified by its system flag."""
    result = await session.execute(select(Action).where(Action.is_system.is_(True)))
    return result.scalars().first()


async def ensure_request_action(session: AsyncSession) -> Action | None:
    """Seed the built-in Request action if it is missing.

    Idempotent. It carries no download client and no quality profile — it never
    touches Prowlarr, so the global preferred-indexer setting does not apply to
    it either. Permissions are granted through the normal ``permissions`` table,
    exactly like any admin-defined action.
    """
    existing = await get_request_action(session)
    if existing is not None:
        return existing

    clash = await session.execute(select(Action).where(Action.name == REQUEST_ACTION_NAME))
    if clash.scalars().first() is not None:
        # An admin-defined action already holds the reserved name. Leave it
        # alone rather than mangling their configuration; /request will report
        # itself unavailable until the name is freed.
        logger.error(
            "cannot seed the built-in Request action: a non-system action is already"
            " named %r. Rename it to enable /request.",
            REQUEST_ACTION_NAME,
        )
        return None

    action = Action(
        name=REQUEST_ACTION_NAME,
        is_system=True,
        download_client_id=None,
        quality_profile_id=None,
    )
    session.add(action)
    await session.flush()
    logger.info("seeded the built-in Request action (id=%s)", action.id)
    return action
