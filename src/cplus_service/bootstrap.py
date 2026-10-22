"""First-run seeding.

The built-in Request actions (one for films, one for TV) must always exist, so
they are seeded idempotently on startup rather than by a data migration — that
way a fresh install gains them without anyone running anything.

The starter quality profile is seeded the same way, for a different reason:
every Prowlarr-backed action needs a profile, so an install with none has a
dead end on the Actions page. See :func:`ensure_default_quality_profile`.

The default home shelf is seeded for a third reason: it is what the app itself
falls back to when it has no shelves of its own, so a fresh install's Home tab
starts showing the same thing rather than an empty list. See
:func:`ensure_default_home_shelf`.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Action, Config, QualityProfile
from .quality.models import default_profile

logger = logging.getLogger(__name__)

#: The button title both built-in Request actions are seeded with, and the one
#: word the admin UI keeps for them. Nothing *looks it up* — the server finds
#: these actions by ``request_type`` and clients tell them apart by the ``kind``
#: field in the actions payload — so an admin may retitle them freely. The word
#: is reserved for a different reason: it names what this service does when it
#: files a request in Seerr, so no other action may wear it and mislead whoever
#: presses it.
REQUEST_ACTION_NAME = "Request"

#: What each built-in Request action is called when seeded. Two names because
#: names are unique and both appear side by side on the Users page; the button
#: on the television reads :data:`REQUEST_ACTION_NAME` for both.
REQUEST_ACTION_NAMES = {"movie": "Request Movies", "tv": "Request TV"}

#: The starter profile's name. Like every other name here it is a label and not
#: a contract — an ordinary profile an admin may rename, edit or delete once
#: they have one of their own.
DEFAULT_PROFILE_NAME = "All"


async def get_request_action(session: AsyncSession, media_type: str) -> Action | None:
    """The built-in Request action for ``"movie"`` or ``"tv"``."""
    result = await session.execute(select(Action).where(Action.request_type == media_type))
    return result.scalars().first()


async def ensure_request_actions(session: AsyncSession) -> None:
    """Seed whichever built-in Request action is missing.

    Idempotent. They carry no download client and no quality profile — they
    never touch Prowlarr, so the global preferred-indexer setting does not apply
    to them either. Permissions are granted through the normal ``permissions``
    table, exactly like any admin-defined action.

    An install that already had a single Request action was split into these
    two by migration ``a7c3e5f19b28``, which also carried over every grant;
    this only ever seeds on a fresh install.
    """
    for media_type, name in REQUEST_ACTION_NAMES.items():
        if await get_request_action(session, media_type) is not None:
            continue

        clash = await session.execute(select(Action).where(Action.name == name))
        if clash.scalars().first() is not None:
            # Names are unique and an admin-defined action already holds this
            # one. Leave their configuration alone rather than mangling it, and
            # say what to do.
            logger.error(
                "cannot seed the built-in %s request action: an action is already"
                " named %r. Rename that one to enable /request for %s.",
                media_type,
                name,
                media_type,
            )
            continue

        action = Action(
            name=name,
            display_title=REQUEST_ACTION_NAME,
            is_system=True,
            request_type=media_type,
            download_client_id=None,
            quality_profile_id=None,
        )
        session.add(action)
        await session.flush()
        logger.info("seeded the built-in %r action (id=%s)", name, action.id)


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


def upnext_shelf() -> dict[str, object]:
    """A fresh "Continue Watching" shelf, in ``HomeShelfDataModel``'s own shape.

    Mirrors ``HomeShelfDataModel.upNext()`` in CanopyPlus exactly — same path,
    same style, same fields and no others. Earlier revisions of this service
    also stamped a per-shelf ``modifiedAt`` here, guessing at what a future
    sync layer would need; CanopyPlus's own ``HomeSettings`` (see
    ``db.models.Config.home_modified_at``) settled that differently — one
    ``modifiedAt`` for the whole Home document, not one per shelf — so this
    went back to matching ``HomeShelfDataModel`` exactly.
    """
    return {
        "id": str(uuid.uuid4()),
        "title": "Continue Watching",
        "description": "Continue Watching / On Deck",
        "path": "/library/onDeck",
        "discoverHubKey": None,
        "style": "card",
        "titleOnly": False,
    }


async def ensure_default_home_shelf(session: AsyncSession, config: Config) -> bool:
    """Seed one "Continue Watching" shelf when the Home tab has none.

    Mirrors ``AppSettings``'s own fallback, so a fresh install's Home tab is
    never an empty list an admin has to fill from nothing. Seeded only while
    ``home_shelves`` is empty — the same rule :func:`ensure_default_quality_profile`
    uses for profiles: an admin who removes every shelf is back in exactly the
    dead end seeding exists to prevent, so the next startup seeds the default
    one again rather than leaving the tab empty.
    """
    if config.home_shelves:
        return False

    config.home_shelves = [upnext_shelf()]
    logger.info("seeded the default \"Continue Watching\" home shelf")
    return True


async def ensure_default_carousel(session: AsyncSession, config: Config) -> bool:
    """Seed the hero carousel with a "Continue Watching" default when unset.

    The carousel is a shelf-shaped piece of Home config like any other, so a
    fresh install's Home tab is never missing "the one" it's meant to always
    have — it just starts off shown or not per ``home_carousel_enabled``'s
    own default, exactly as :func:`ensure_default_home_shelf` does for the
    shelf list.
    """
    if config.home_carousel is not None:
        return False

    config.home_carousel = upnext_shelf()
    logger.info('seeded the default "Continue Watching" carousel')
    return True


async def ensure_default_top_shelf(session: AsyncSession, config: Config) -> bool:
    """Seed the tvOS Top Shelf entry with a "Continue Watching" default when unset.

    Same reasoning as :func:`ensure_default_carousel` — Top Shelf has no
    enabled flag to begin disabled behind, so this is the only seeding it
    needs.
    """
    if config.home_top_shelf is not None:
        return False

    config.home_top_shelf = upnext_shelf()
    logger.info('seeded the default "Continue Watching" Top Shelf entry')
    return True
