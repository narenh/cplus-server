"""Request and response bodies for the public API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"

#: Bounds on a client-supplied release year. Wide enough for the whole of
#: cinema and then some — the point is to reject a mis-mapped field, not to
#: adjudicate what year a film may have come out in.
MIN_MEDIA_YEAR = 1870
MAX_MEDIA_YEAR = 2999


class MediaIdentity(BaseModel):
    """What the notification's first line is built from.

    Optional, and only ever used for display. The client is holding the real
    title and year already — it is showing them on the detail page the button
    was pressed on — so sending them along saves the server either guessing
    from a scene release name or making a TMDB call on the path of a request
    that has nothing else to wait for.

    Omitting them is fine and stays fine: a grab falls back to parsing the
    release title, and a request falls back to naming the TMDB id. Both read
    worse than the real thing, which is the only reason to send it.
    """

    media_title: str | None = Field(default=None, max_length=512)
    media_year: int | None = Field(default=None, ge=MIN_MEDIA_YEAR, le=MAX_MEDIA_YEAR)


class ReleaseFields(MediaIdentity):
    """The release identity shared by every way of grabbing one.

    Comes straight back from the search stream the client was already sent.
    ``indexer_id`` is what Prowlarr needs to identify the listing;
    ``release_title`` and ``size_bytes`` are recorded on the ``grabs`` row so
    the history is readable without re-querying an indexer for a listing that
    may no longer exist. ``size_bytes`` is optional because not every indexer
    reports a size — an unknown size is a real state, not an omission.
    """

    model_config = ConfigDict(extra="forbid")

    release_guid: str = Field(min_length=1)
    indexer_id: int
    release_title: str = Field(min_length=1)
    size_bytes: int | None = Field(default=None, ge=0)


class GrabRequest(ReleaseFields):
    """``POST /grab`` — tvOS only.

    ``action_id`` names the download client indirectly: the action carries it,
    and the caller must have been granted that action. Authenticated from the
    stored token mapping, no outbound call. The admin app's action-free grab —
    naming a download client directly during a request approval — is a
    different caller with different auth and lives at
    ``POST /manager/grab`` instead; see :class:`ManagerGrabRequest`.
    """

    action_id: int


class ManagerGrabRequest(ReleaseFields):
    """``POST /manager/grab`` — the admin app's action-free grab.

    Actions exist to give tvOS buttons a label and a recommendation, which an
    admin picking a specific release during a request approval does not need —
    so the download client is named directly and no action is involved.
    Restricted to callers who can manage requests, checked against Seerr live.
    """

    download_client_id: int


class GrabResponse(BaseModel):
    success: bool
    message: str | None = None
    grab_id: int | None = None


class RequestCreate(MediaIdentity):
    """``POST /request``.

    ``tmdb_id`` is a TMDB id, not an IMDB id — Seerr's request endpoint is
    TMDB-keyed while the rest of this service is IMDB-keyed. The client already
    holds a TMDB id from Plex metadata and sends it directly.

    ``seasons`` is required and non-empty for ``tv`` and rejected for ``movie``.
    Season ``0`` means specials. We pass the array through to Seerr exactly as
    given and never substitute the literal ``"all"``, which would silently drop
    specials.
    """

    model_config = ConfigDict(extra="forbid")

    tmdb_id: int
    type: Literal["movie", "tv"]
    seasons: list[int] | None = None

    @model_validator(mode="after")
    def _check_seasons(self) -> RequestCreate:
        if self.type == MEDIA_TYPE_TV:
            if not self.seasons:
                raise ValueError("seasons is required and must be non-empty when type is 'tv'")
            if any(season < 0 for season in self.seasons):
                raise ValueError("season numbers cannot be negative")
        elif self.seasons is not None:
            raise ValueError("seasons is not applicable when type is 'movie'")
        return self


class RequestResponse(BaseModel):
    success: bool
    message: str | None = None
    request_id: int | None = None


class PushDeviceRegistration(BaseModel):
    """``POST /manager/push-devices`` — an app offering its APNs device token.

    ``environment`` is a property of the token, not a preference: a token from
    a development build only works against Apple's sandbox host and a
    TestFlight or App Store build only against production. The app knows which
    one it is (``aps-environment`` in its entitlements) and tells us, because
    the server has no way to tell by looking.

    Sent on every launch, not just the first: Apple can reissue a device token
    at any time, and a re-registration of an unchanged one is how we know the
    app is still installed.
    """

    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(min_length=1, max_length=200, pattern=r"^[0-9a-fA-F]+$")
    environment: Literal["sandbox", "production"] = "production"
    device_name: str | None = Field(default=None, max_length=256)


class PushDeviceResponse(BaseModel):
    success: bool
    message: str | None = None
