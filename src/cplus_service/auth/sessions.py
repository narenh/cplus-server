"""Browser sessions for the admin webui.

Only the webui has sessions.  tvOS presents its Plex token on every request and
never receives one of these.

The cookie carries an opaque 256-bit random token; everything else lives in the
``admin_sessions`` table.  That means no signing secret to generate, store or
rotate, and revoking a session is a row delete.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AdminSession, User

SESSION_COOKIE_NAME = "cplus_session"
SESSION_TOKEN_BYTES = 32

#: How long an admin stays signed in. Enforced server-side as well as by the
#: cookie's own max-age — a cookie lifetime alone is only a request from the
#: server to the browser, and an expired row would otherwise stay valid forever
#: to anyone who kept the token.
SESSION_TTL = timedelta(days=30)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Attach the session cookie, marking it ``Secure`` when the request was HTTPS.

    Detected per request rather than configured, so there is no flag to get
    wrong: deployed behind a TLS-terminating proxy the cookie is Secure, and a
    local ``python -m cplus_service`` over plain HTTP still works. This relies
    on uvicorn running with ``proxy_headers`` so ``request.url.scheme`` reflects
    ``X-Forwarded-Proto`` rather than the proxy's plaintext hop.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )


def _is_expired(record: AdminSession, *, now: datetime) -> bool:
    created = record.created_at
    if created.tzinfo is None:
        # SQLite hands back naive datetimes; they were written as UTC.
        created = created.replace(tzinfo=UTC)
    return now - created > SESSION_TTL


async def create_session(session: AsyncSession, user_id: int) -> str:
    """Open a browser session for an admin and return its cookie value."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    session.add(AdminSession(token=token, user_id=user_id))
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str | None) -> User | None:
    """Look up the admin behind a session cookie, refreshing its last-seen stamp.

    An expired session reads as signed out. The row is deliberately *not*
    deleted here: callers reject by raising, which rolls the request's
    transaction back, so a delete on this path would never be committed.
    :func:`purge_expired_sessions` sweeps them at startup instead.
    """
    if not token:
        return None

    record = await session.get(AdminSession, token)
    if record is None:
        return None

    now = datetime.now(UTC)
    if _is_expired(record, now=now):
        return None

    record.last_seen_at = now
    return await session.get(User, record.user_id)


async def purge_expired_sessions(session: AsyncSession) -> int:
    """Drop every session past its TTL. Called on startup to keep the table tidy."""
    cutoff = datetime.now(UTC) - SESSION_TTL
    result = await session.execute(
        delete(AdminSession).where(AdminSession.created_at < cutoff)
    )
    return result.rowcount or 0


async def destroy_session(session: AsyncSession, token: str | None) -> None:
    """Log a browser out.  Unknown tokens are a no-op, not an error."""
    if token:
        await session.execute(delete(AdminSession).where(AdminSession.token == token))


async def destroy_sessions_for_user(session: AsyncSession, user_id: int) -> None:
    """Revoke every session a user holds.

    Used when an admin removes a user, so their browser access ends at once
    rather than at the next expiry.
    """
    await session.execute(delete(AdminSession).where(AdminSession.user_id == user_id))


async def destroy_other_sessions(session: AsyncSession, *, keep_token: str | None) -> None:
    """Revoke every browser session except one.

    Used when the configured Seerr instance changes: every session's ADMIN bit
    and identity were resolved against the old instance. ``keep_token`` lets the
    admin who is making the change right now — already verified, this request —
    stay signed in instead of being logged out by their own edit.
    """
    stmt = delete(AdminSession)
    if keep_token is not None:
        stmt = stmt.where(AdminSession.token != keep_token)
    await session.execute(stmt)


async def count_sessions(session: AsyncSession) -> int:
    result = await session.execute(select(AdminSession.token))
    return len(result.scalars().all())
