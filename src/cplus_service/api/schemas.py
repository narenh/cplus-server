"""Request and response bodies for the public API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


class ActionOut(BaseModel):
    """One button in the client's UI.

    Deliberately just an id and a label: the client has no use for the download
    client or the quality profile behind an action, and they are not its
    business. The client routes on the name — ``"Request"`` posts to
    ``/request``, everything else posts to ``/grab`` — which is safe because a
    system action cannot be renamed.
    """

    id: int
    name: str


class ActionsResponse(BaseModel):
    actions: list[ActionOut]


class GrabRequest(BaseModel):
    """``POST /grab``.

    Exactly one of ``action_id`` or ``download_client_id`` — they select the two
    ways to grab:

    ``action_id``
        The tvOS path. The action names the download client, and the caller must
        have been granted that action. Authenticated from the stored token
        mapping, no outbound call.

    ``download_client_id``
        The admin app's path. Actions exist to give tvOS buttons a label and a
        recommendation, which an admin picking a specific release during an
        approval does not need — so the client is named directly and no action
        is involved. Restricted to callers who can manage requests, checked
        against Seerr live.

    Every release field comes straight back from the search stream the client
    was already sent. ``indexer_id`` is what Prowlarr needs to identify the
    listing; ``release_title`` and ``size_bytes`` are recorded on the ``grabs``
    row so the history is readable without re-querying an indexer for a listing
    that may no longer exist. ``size_bytes`` is optional because not every
    indexer reports a size — an unknown size is a real state, not an omission.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: int | None = None
    download_client_id: int | None = None
    release_guid: str = Field(min_length=1)
    indexer_id: int
    release_title: str = Field(min_length=1)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_target(self) -> GrabRequest:
        if (self.action_id is None) == (self.download_client_id is None):
            raise ValueError(
                "provide exactly one of action_id or download_client_id"
            )
        return self


class GrabResponse(BaseModel):
    success: bool
    message: str | None = None
    grab_id: int | None = None


class RequestCreate(BaseModel):
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
