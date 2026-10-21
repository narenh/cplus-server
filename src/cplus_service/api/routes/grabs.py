"""``GET /grabs/{grab_id}`` — how a tracked grab is getting on, as a long poll.

The app grabs with ``POST /grab``, keeps the ``grab_id`` it gets back, and —
when ``GET /capabilities`` says ``grab_tracking`` — asks here instead of running
its own timers::

    GET /grabs/17                      → answers at once, version 1
    GET /grabs/17?since=1&wait=25      → answers the moment anything changes,
                                         or after 25 s with the same state
    …until phase is "ready" (play rating_key) or "failed" (show error)

``since`` is the last ``version`` the app saw. A state newer than that is
answered at once; otherwise the request waits up to ``wait`` seconds (at most
25, under the usual 30-second proxy and URLSession timeouts) for a change.
Omitting ``since`` always answers at once.

Authenticated like ``POST /grab``, from the stored token mapping, and a caller
sees only their own grabs: anyone else's is a 404, exactly as if it did not
exist, so grab ids cannot be probed. A grab that was never tracked — made while
tracking was off — is a 404 as well, since there is nothing to report.

After a restart the live state is gone. A grab whose row says it finished
answers from the row; one that was still in progress answers ``failed``, since
nothing is tracking it any more and nothing will.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from ...db.models import Grab
from ...tracking.state import GrabPhase, GrabProgress
from ..deps import CachedUserDep, DbDep, StateDep

router = APIRouter(tags=["client"])

#: The longest a long poll is held open, in seconds.
MAX_WAIT_SECONDS = 25.0

RESTARTED_ERROR = "cplus restarted while following this download. Check Plex for it."


def _from_row(grab: Grab) -> GrabProgress:
    """A state rebuilt from the grab's row, for a grab no longer held in memory."""
    try:
        phase = GrabPhase(grab.tracking_phase or "")
    except ValueError:
        phase = GrabPhase.FAILED
    error = grab.tracking_error
    if not phase.finished:
        phase, error = GrabPhase.FAILED, RESTARTED_ERROR
    return GrabProgress(
        grab_id=grab.id,
        user_id=grab.user_id,
        title=grab.release_title,
        phase=phase,
        torrent_hash=grab.torrent_hash,
        rating_key=grab.rating_key if phase is GrabPhase.READY else None,
        error=error if phase is GrabPhase.FAILED else None,
    )


@router.get("/grabs/{grab_id}")
async def grab_status(
    grab_id: int,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    since: Annotated[int, Query()] = 0,
    wait: Annotated[float, Query(ge=0)] = 0.0,
) -> dict[str, Any]:
    """One tracked grab's phase, download numbers and, once ready, its Plex rating key."""
    not_found = HTTPException(status.HTTP_404_NOT_FOUND, "No tracked grab with that id.")

    progress = state.grab_tracker.get(grab_id)
    if progress is None:
        grab = await db.get(Grab, grab_id)
        if grab is None or grab.user_id != user.id or grab.tracking_phase is None:
            raise not_found
        return _from_row(grab).as_json()

    if progress.user_id != user.id:
        raise not_found

    # End the transaction before waiting. Authenticating wrote to the caller's
    # token mapping, and SQLite would hold that write lock — blocking every
    # other request that writes — for as long as this poll stays open.
    await db.commit()
    await progress.wait_newer(since, min(wait, MAX_WAIT_SECONDS))
    return progress.as_json()
