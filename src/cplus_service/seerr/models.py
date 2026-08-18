"""Typed views over the bits of Seerr's API we consume.

Seerr (Overseerr/Jellyseerr) is used for exactly two things: validating a Plex
token into a user identity, and creating requests on that user's behalf.  No
library sync, no media lookups, nothing else.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SeerrPermission(IntEnum):
    """Seerr's permission bitmask.

    Only :attr:`ADMIN` matters to this service, and it is checked as a bit —
    ``permissions & ADMIN`` — not by comparing ``id == 1``.  Seerr grants admin
    rights by this bit, and the owner account is not guaranteed to be user 1.
    """

    NONE = 0
    ADMIN = 2
    MANAGE_SETTINGS = 4
    MANAGE_USERS = 8
    MANAGE_REQUESTS = 16
    REQUEST = 32


class SeerrUser(BaseModel):
    """The user object Seerr returns from ``/api/v1/auth/plex``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    permissions: int = 0
    email: str | None = None
    plex_username: str | None = Field(default=None, alias="plexUsername")
    username: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")

    @property
    def is_admin(self) -> bool:
        """Whether Seerr's ADMIN bit is set."""
        return bool(self.permissions & SeerrPermission.ADMIN)

    @property
    def best_username(self) -> str:
        """Whatever Seerr gave us that is fit to show an admin, in preference order."""
        for candidate in (self.plex_username, self.username, self.display_name, self.email):
            if candidate:
                return candidate
        return f"seerr-user-{self.id}"


class SeerrAuth(BaseModel):
    """A validated Plex token: who it belongs to, plus the Seerr session it opened.

    ``session_cookie`` is what lets us create a request *as that user* rather
    than as the admin — it is short-lived and never persisted.
    """

    model_config = ConfigDict(frozen=True)

    user: SeerrUser
    session_cookie: str | None = None


class SeerrRequestResult(BaseModel):
    """Outcome of a request creation.

    ``raw`` is kept whole so the activity log can record exactly what Seerr
    said, including the reason for a rejection (quota exceeded, already
    requested, and so on).
    """

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    status: int | None = None
    media_type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
