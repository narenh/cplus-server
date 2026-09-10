"""``GET``/``PUT /home`` — one user's home screen, synced both ways.

CanopyPlus keeps its whole home screen as a single versioned document and
merges it whole-document last-write-wins on one ``modifiedAt``. This is the
other half of that: the same document, per user, so a person's shelves follow
them to every Apple TV they sign in on — and so an admin editing that person's
Home in the web UI reaches those same devices.

**Which document is "theirs" depends on whether they have ever edited one.**
A user with no ``UserHomeSettings`` row of their own is still tracking the
admin's global default, and ``GET`` hands them exactly that. The fork happens
on the first accepted ``PUT`` (or the first time an admin opens their Home in
the web UI), and from then on the two are independent in both directions. See
:mod:`cplus_service.home` for the projection itself and
:mod:`..admin.user_home` for the admin's side of the same rows.

**The merge rule, in full.** ``PUT`` carries a whole document and its
``modifiedAt``; the server compares it against the stamp on whatever document
applies to that user today:

* strictly newer — the client wins. Stored verbatim, ``200``.
* equal — nothing to do. Not written, ``200``. This is the ordinary case for a
  client re-pushing after a reconnect, and treating it as a conflict would make
  an idempotent push look like a failure.
* older, or the client is pushing against a document that has been edited since
  it last synced — the server wins, ``409``.

**Both outcomes answer with the winning document, in the identical shape.**
A client applies the body it gets back either way and is immediately consistent;
it never needs a second call to find out what it lost to, and the ``409`` is
what tells it to stop treating its local copy as unsynced. That is the whole
reconciliation protocol.

Cache-only auth on both, like ``/titles/{imdb_id}/actions`` and ``/grab``:
tvOS calls ``GET /register`` on launch anyway, which is what primes the mapping.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from ...db.session import get_config
from ...home import apply, as_utc, document, effective_home, get_or_create_home
from ..deps import CachedUserDep, DbDep
from ..schemas import HomeDocument

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


@router.get("/home")
async def read_home(db: DbDep, user: CachedUserDep) -> dict[str, object]:
    """This user's whole home screen, ready to decode into ``HomeSettings``."""
    return document(await effective_home(db, user.id))


@router.put("/home")
async def write_home(
    db: DbDep, user: CachedUserDep, body: HomeDocument, response: Response
) -> dict[str, object]:
    """Merge a client's document in, and answer with whichever one won."""
    current = await effective_home(db, user.id)
    stored = as_utc(current.home_modified_at)
    incoming = as_utc(body.modifiedAt)

    if stored is not None and incoming is not None and incoming < stored:
        response.status_code = status.HTTP_409_CONFLICT
        return document(current)

    if stored is not None and incoming == stored:
        # An unchanged re-push. Writing it would be a no-op that still forked
        # a user off the global default for no reason.
        return document(current)

    config = await get_config(db)
    own = await get_or_create_home(db, user.id, config)
    apply(own, body)
    logger.info("Home updated by user %s (%s)", user.id, user.plex_username)
    return document(own)
