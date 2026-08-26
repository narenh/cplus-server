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
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AdminSession, User

SESSION_COOKIE_NAME = "cplus_session"
SESSION_TOKEN_BYTES = 32

#: How long an admin stays signed in. Enforced server-side as well as by the
#: cookie's own max-age — a cookie lifetime alone is only a request from the
#: server to the browser, and an expired row would otherwise stay valid forever
#: to anyone who kept the token.
SESSION_TTL = timedelta(days=7)


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


async def destroy_all_sessions(session: AsyncSession) -> None:
    """Revoke every browser session, with no exceptions.

    Used when the configured Seerr instance changes: every session's ADMIN bit
    and identity were resolved against the old instance. There is no carve-out
    for the admin who made the change, because there is no longer a request in
    which to make one — the URL now comes from the environment, so the change
    lands at startup, before anyone is signed in.
    """
    await session.execute(delete(AdminSession))
