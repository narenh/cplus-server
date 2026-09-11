"""Request and response bodies for the public API."""

from __future__ import annotations

from datetime import datetime
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
    a moderator picking a specific release during a request approval — is a
    different caller with different auth and lives at
    ``POST /manager/grab`` instead; see :class:`ManagerGrabRequest`.
    """

    action_id: int


class ManagerGrabRequest(ReleaseFields):
    """``POST /manager/grab`` — the admin app's action-free grab.

    Actions exist to give tvOS buttons a label and a recommendation, which an
    admin picking a specific release during a request approval does not need,
    so no action is involved. Restricted to callers who can manage requests,
    checked against Seerr live.

    Adds nothing to :class:`ReleaseFields`: the release is the whole request.
    Which download client it lands in is not the caller's to choose — Prowlarr
    is asked for its default — so this is a distinct type from
    :class:`GrabRequest` only in what it *refuses*, an ``action_id``, which
    ``extra="forbid"`` turns into a 422 rather than a silently ignored field.
    """


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


#: A ceiling on a pushed shelf list. The admin UI imposes no limit of its own
#: and no real Home comes close to this, so it is not a product rule — it is
#: the bound that stops one authenticated client from parking an unbounded blob
#: in someone's row. The floor of one is a real rule, though, and both ends
#: already enforce it: CanopyPlus disables shelf removal below two, and
#: ``admin.libraries`` refuses to drop the last one.
MAX_HOME_SHELVES = 100

#: ``HomeShelfDataModel.ShelfStyle`` in CanopyPlus, exactly.
SHELF_STYLES = ("poster", "card", "hero", "square", "tvEpisode")


class HomeShelf(BaseModel):
    """One shelf, in CanopyPlus's own ``HomeShelfDataModel`` shape.

    Field names are the app's Codable names rather than this service's usual
    snake_case, deliberately: the whole point of the Home document is that it
    crosses the wire needing no translation at either end.

    ``extra="forbid"`` matters more here than it does elsewhere. A shelf the
    client invents a field on would otherwise be stored verbatim and handed
    back to the *admin* UI, which builds its editors from these dicts and knows
    nothing of it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(max_length=256)
    description: str = Field(max_length=1024)
    path: str = Field(min_length=1, max_length=1024)
    discoverHubKey: str | None = Field(default=None, max_length=512)
    style: Literal[SHELF_STYLES]
    titleOnly: bool


class HomeDocument(BaseModel):
    """``PUT /home`` — one whole ``HomeSettings`` document from a client.

    The mirror of :func:`cplus_service.home.document`, and deliberately not the
    same object: what leaves is projected from whatever is on file and tolerates
    gaps in it, while what arrives is a complete document from a client that
    holds one and is rejected outright if it is not.

    ``modifiedAt`` is **required**, unlike on the way out. Absence on the way
    out means "never edited"; a client with a never-edited document has nothing
    to push and should not be pushing, so absence on the way in is a bug rather
    than a state — and a document with no stamp could not be merged against one
    that has one anyway.
    """

    model_config = ConfigDict(extra="forbid")

    carouselEnabled: bool
    carouselIncludeOnDeck: bool
    carouselShelf: HomeShelf
    homeShelves: list[HomeShelf] = Field(min_length=1, max_length=MAX_HOME_SHELVES)
    topShelf: HomeShelf
    modifiedAt: datetime


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
