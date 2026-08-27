"""First-run seeding.

The built-in Request action must always exist, so it is seeded idempotently on
startup rather than by a data migration — that way an existing deployment gains
it on upgrade without anyone running anything.

The starter quality profile is seeded the same way, for a different reason:
every Prowlarr-backed action needs a profile, so an install with none has a
dead end on the Actions page. See :func:`ensure_default_quality_profile`.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Action, QualityProfile
from .quality.models import default_profile

logger = logging.getLogger(__name__)

#: What the built-in action is called when seeded, and the one word the admin
#: UI keeps for it. Nothing *looks it up* by name — the server finds this action
#: by ``is_system`` and clients tell it apart by the ``kind`` field in the
#: actions payload — so an admin may rename it freely. The word is reserved for
#: a different reason: it names what this service does when it files a request
#: in Seerr, so no other action may wear it and mislead whoever presses it.
REQUEST_ACTION_NAME = "Request"

#: The starter profile's name. Like every other name here it is a label and not
#: a contract — an ordinary profile an admin may rename, edit or delete once
#: they have one of their own.
DEFAULT_PROFILE_NAME = "All"


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
        # Names are unique and an admin-defined action already holds this one.
        # Reachable only on an install that predates the built-in action and
        # already had one of its own called "Request"; leave their
        # configuration alone rather than mangling it, and say what to do.
        logger.error(
            "cannot seed the built-in request action: a non-system action is already"
            " named %r. Rename that one to enable /request.",
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


async def ensure_default_quality_profile(session: AsyncSession) -> QualityProfile | None:
    """Seed a starter quality profile when the install has none.

    Every Prowlarr-backed action needs a profile, so a fresh install's Actions
    page is a dead end until one exists — an admin who just connected Prowlarr
    is sent off to build a rule list before they can create the single button
    they came for. Seeding one removes that step: they can create an action
    immediately and come back to shape the rules once they know what they want.

    The profile is called "All" because it **filters nothing** — no candidate
    is ever eliminated by it. It is not empty, though: it carries the
    conventional ranking (see
    :func:`~cplus_service.quality.models.default_profile`), so its pick is the
    best available copy rather than whichever release an indexer happened to
    list first, which is what a profile with no rules at all would give.

    Seeded only when the table is empty, so it never appears alongside an
    admin's own profiles and never resurrects one they deleted — unless they
    deleted every profile, in which case the install is back in exactly the
    dead end this exists to prevent and a starter is the right answer again.
    """
    count = await session.execute(select(func.count()).select_from(QualityProfile))
    if count.scalar_one():
        return None

    schema = default_profile(DEFAULT_PROFILE_NAME)
    profile = QualityProfile(
        name=schema.name,
        rules=[rule.model_dump(mode="json") for rule in schema.rules],
    )
    session.add(profile)
    await session.flush()
    logger.info(
        "seeded the starter quality profile %r (id=%s)", profile.name, profile.id
    )
    return profile
