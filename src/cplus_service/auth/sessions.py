"""Browser sessions for the admin webui.

Only the webui has sessions.  tvOS presents its Plex token on every request and
never receives one of these.

The cookie carries an opaque 256-bit random token; everything else lives in the
``admin_sessions`` table.  That means no signing secret to generate, store or
rotate, and revoking a session is a row delete.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AdminSession, User

SESSION_COOKIE_NAME = "cplus_session"
SESSION_TOKEN_BYTES = 32


async def create_session(session: AsyncSession, user_id: int) -> str:
    """Open a browser session for an admin and return its cookie value."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    session.add(AdminSession(token=token, user_id=user_id))
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str | None) -> User | None:
    """Look up the admin behind a session cookie, refreshing its last-seen stamp."""
    if not token:
        return None

    record = await session.get(AdminSession, token)
    if record is None:
        return None

    record.last_seen_at = datetime.now(UTC)
    return await session.get(User, record.user_id)


async def destroy_session(session: AsyncSession, token: str | None) -> None:
    """Log a browser out.  Unknown tokens are a no-op, not an error."""
    if token:
        await session.execute(delete(AdminSession).where(AdminSession.token == token))


async def destroy_sessions_for_user(session: AsyncSession, user_id: int) -> None:
    """Revoke every session a user holds — for stage 3's admin UI."""
    await session.execute(delete(AdminSession).where(AdminSession.user_id == user_id))


async def count_sessions(session: AsyncSession) -> int:
    result = await session.execute(select(AdminSession.token))
    return len(result.scalars().all())
