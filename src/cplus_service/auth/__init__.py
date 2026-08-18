"""Authentication: the tvOS Plex-token mapping and the webui browser session."""

from .identity import authenticate_plex_token, upsert_user
from .plex_cache import (
    count_tokens,
    forget_token,
    forget_user_tokens,
    remember_token,
    resolve_token,
    token_fingerprint,
)
from .sessions import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    create_session,
    destroy_session,
    destroy_sessions_for_user,
    purge_expired_sessions,
    resolve_session,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "authenticate_plex_token",
    "count_tokens",
    "create_session",
    "destroy_session",
    "destroy_sessions_for_user",
    "forget_token",
    "forget_user_tokens",
    "purge_expired_sessions",
    "remember_token",
    "resolve_session",
    "resolve_token",
    "token_fingerprint",
    "upsert_user",
]
