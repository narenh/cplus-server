"""The Plex-token → local-user mapping that backs tvOS auth.

tvOS already holds a user-scoped Plex token, so there is no login step and no
session token for it. Instead:

* ``GET /register`` validates the Plex token against Seerr for real, upserts
  the local user, and records the mapping here.
* ``/titles/{imdb_id}/actions``, ``/search`` and ``/grab`` read this mapping
  only. They never call out to Plex or Seerr, which is what keeps them fast.
* ``/request`` deliberately bypasses it and always validates live, because it
  acts against Seerr as the user.

**Persisted, not in-memory.** A restart used to empty the mapping and 401 every
client until its next launch; entries now live in the database and survive one.
The cost is a local SQLite read on the fast paths — negligible, and those
requests already open a session for the activity log.

Tokens are stored as a SHA-256 fingerprint rather than in the clear, so this
table cannot yield a working Plex credential even if the database file leaks.

Deliberately absent:

* No expiry. An entry stays valid until that user's next ``/register`` call
  overwrites it, or until the user is deleted, which cascades. The tradeoff is
  that a Plex token revoked upstream keeps working on ``/titles/{imdb_id}/actions``,
  ``/search`` and ``/grab`` until one of those happens — removing the user in
  the admin UI is the immediate lever.
* No revocation push. Permission changes take effect at the user's next
  ``/register`` call; that is an accepted tradeoff, not a bug.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PlexTokenSession, User


def token_fingerprint(plex_token: str) -> str:
    """Stable, non-reversible key for a Plex token."""
    return hashlib.sha256(plex_token.encode("utf-8")).hexdigest()


async def remember_token(session: AsyncSession, plex_token: str, user: User) -> None:
    """Record (or refresh) the mapping for a token that just validated."""
    fingerprint = token_fingerprint(plex_token)
    existing = await session.get(PlexTokenSession, fingerprint)

    if existing is None:
        session.add(PlexTokenSession(token_fingerprint=fingerprint, user_id=user.id))
    else:
        existing.user_id = user.id
        existing.last_seen_at = datetime.now(UTC)
    await session.flush()


async def resolve_token(session: AsyncSession, plex_token: str) -> User | None:
    """The user a token belongs to, or ``None`` if it was never validated here."""
    record = await session.get(PlexTokenSession, token_fingerprint(plex_token))
    if record is None:
        return None

    record.last_seen_at = datetime.now(UTC)
    return await session.get(User, record.user_id)


async def forget_all_tokens(session: AsyncSession) -> None:
    """Drop every cached Plex-token mapping, for every user.

    Used when the configured Seerr instance changes: every existing mapping —
    and the permissions it implies — was resolved against the *old* instance,
    and nothing here records which instance issued it. Wholesale is simpler and
    cheaper than tagging each row with an instance fingerprint; the cost is one
    extra live ``/register`` round trip per device on its next call, which is
    exactly the recovery path already built for an unrecognised token.
    """
    await session.execute(delete(PlexTokenSession))


async def count_tokens(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(PlexTokenSession))
    return int(result.scalar_one())
