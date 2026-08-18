"""Authentication: the tvOS Plex-token cache and the webui browser session."""

from .identity import authenticate_plex_token, upsert_user
from .plex_cache import CachedUser, PlexTokenCache, token_fingerprint
from .sessions import (
    SESSION_COOKIE_NAME,
    create_session,
    destroy_session,
    destroy_sessions_for_user,
    resolve_session,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "CachedUser",
    "PlexTokenCache",
    "authenticate_plex_token",
    "create_session",
    "destroy_session",
    "destroy_sessions_for_user",
    "resolve_session",
    "token_fingerprint",
    "upsert_user",
]
