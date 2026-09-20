"""``POST /webhooks/seerr`` — requests filed in Seerr rather than through here.

``POST /request`` covers one direction: a user presses Request in the app, this
service files it with Seerr, logs it and pushes to the admin. Anyone who instead
opens Seerr in a browser and requests there was, until this endpoint existed,
invisible — no activity row, no notification, nothing to tell an admin that
something was waiting for them.

Seerr already notifies webhook subscribers about every request. This is the
other end of that: Seerr posts, we record and push, and the admin's phone says
the same thing it would have said for an in-app request.

Three decisions worth stating outright:

**It authenticates on a shared secret, and only that.** Seerr's webhook has one
credential — an ``Authorization`` header whose value the admin types in — so
that is what there is to check. No secret configured means the endpoint refuses
everyone, rather than accepting anonymous posts about who requested what.

**It answers 200 to things it ignores.** Seerr sends a dozen notification types
and this service acts on two of them. Answering an error to the rest would make
Seerr log failures and retry deliveries for events that are being ignored
deliberately, so the body carries ``handled`` and the status stays 200.

**It never invents a user.** A local ``users`` row exists because someone
authenticated through Seerr; it is keyed by Seerr's user id, which the webhook
payload does not carry. So the requester is matched by name, best-effort, and a
miss is not a failure: the request is logged unattributed and the notification
names whoever Seerr said it was.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import ActivityLog, EventType, SeerrRequestNotice, User
from ...db.session import get_config
from ...notify.messages import MediaSummary, user_requested
from ...seerr.webhook import SeerrWebhookEvent, parse_event, split_year
from ..deps import DbDep, StateDep
from ..notifications import schedule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhook"])

#: What a notification says when the payload carried no requester at all. Only
#: reachable through a hand-edited payload template — the stock one always
#: sends a username — and still better than a subtitle that trails off.
UNKNOWN_REQUESTER = "a Seerr user"


@router.post("/seerr")
async def seerr_webhook(
    request: Request,
    db: DbDep,
    state: StateDep,
    background: BackgroundTasks,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Record a request someone filed in Seerr, and tell the admin about it."""
    config = await get_config(db)
    secret = (config.seerr_webhook_secret or "").strip()
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The Seerr webhook is switched off on this server. Generate a secret"
            " on the Configuration tab first.",
        )

    if not _authorised(authorization, secret):
        # Deliberately says nothing about which half was wrong.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Rejected")

    payload = await _body(request)
    event = parse_event(payload)

    if not event.notification_type:
        # The one payload mistake that silences the whole feature. With nothing
        # to dispatch on, every delivery falls through as ignored and Seerr goes
        # on reporting success — so an admin whose template lost this field sees
        # a webhook that works perfectly and notifications that never arrive.
        # Nothing else would ever say so; this line is the only breadcrumb.
        logger.warning(
            "a Seerr webhook arrived carrying no notification_type, so nothing"
            " in it can be acted on. If you have customised Seerr's JSON"
            " payload, it has to keep notification_type."
        )
        return {"handled": False, "reason": "no notification_type"}

    if event.is_test:
        # Seerr's own Test button. Reaching this line is the whole result: the
        # URL resolves, the secret matches, and the admin can stop guessing
        # which of the two was wrong.
        logger.info("Seerr webhook test received")
        return {"handled": False, "reason": "test"}

    if not event.is_request_filed:
        # Debug rather than a warning: an admin is free to tick every type in
        # Seerr, and the ones this does not act on are ignored by design.
        logger.debug("ignoring a Seerr %s webhook", event.notification_type)
        return {"handled": False, "reason": "ignored", "event": event.notification_type}

    # Everything below this line writes, so the duplicate check comes first.
    # A request filed through ``POST /request`` has already been announced by
    # that route, and Seerr will retry a delivery it thinks failed.
    if event.request_id is not None:
        if await db.get(SeerrRequestNotice, event.request_id) is not None:
            return {"handled": False, "reason": "already known"}
        db.add(SeerrRequestNotice(seerr_request_id=event.request_id))
    else:
        # The other payload mistake worth saying out loud. Without an id there
        # is nothing to claim the request by, so this one is announced on every
        # delivery: the ``POST /request`` echo and each of Seerr's retries.
        logger.warning(
            "a Seerr %s webhook carried no request_id, so it cannot be told"
            " apart from a redelivery and may be announced more than once. If"
            " you have customised Seerr's JSON payload, it has to keep"
            " request_id.",
            event.notification_type,
        )

    user = await _match_user(db, event)
    db.add(
        ActivityLog(
            user_id=user.id if user else None,
            event_type=EventType.REQUEST,
            detail=_log_detail(event, matched=user is not None),
        )
    )

    schedule(
        background,
        state,
        user_requested(
            _media_of(event),
            username=event.username or UNKNOWN_REQUESTER,
            **_notification_data(event),
        ),
        # Only meaningful when the requester was matched — an unmatched one has
        # no devices of their own to spare, by definition.
        exclude_user_id=user.id if user else None,
    )

    logger.info(
        "Seerr request %s filed by %r (%s)",
        event.request_id if event.request_id is not None else "?",
        event.username or "unknown",
        "matched" if user else "unmatched",
    )
    return {"handled": True}


def _authorised(presented: str | None, secret: str) -> bool:
    """Whether the header Seerr sent is the secret we issued.

    Seerr sends the configured header value verbatim, so the usual case is a
    bare secret. ``Bearer <secret>`` is accepted too, because that is what an
    admin who has configured a webhook anywhere else will reach for, and
    refusing it would fail in a way nothing on either side explains.
    """
    if not presented:
        return False
    candidate = presented.strip()
    scheme, _, rest = candidate.partition(" ")
    if scheme.lower() == "bearer" and rest.strip():
        candidate = rest.strip()
    return hmac.compare_digest(candidate, secret)


async def _body(request: Request) -> dict[str, Any]:
    """The posted JSON object.

    Read by hand rather than as a typed body parameter so that nothing is parsed
    before the secret has been checked, and so an unparseable body is one 400
    with a reason rather than FastAPI's validation envelope — which Seerr shows
    to nobody, since it only ever records that the delivery failed.
    """
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The webhook body was not valid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The webhook body was not a JSON object"
        )
    return payload


async def _match_user(db: AsyncSession, event: SeerrWebhookEvent) -> User | None:
    """Find the local row for whoever Seerr says filed this, if there is one.

    By name, because the payload carries no user id — see
    :mod:`cplus_service.seerr.webhook`. Both names Seerr offers are tried
    against ``plex_username``, which is itself whichever of Seerr's several
    names was best at sign-in (:attr:`~cplus_service.seerr.models.SeerrUser.best_username`),
    so an install where one user shows up under their email and another under
    their Plex handle still matches both.

    Case-insensitive, and username before email when both hit different rows —
    the username is the one Seerr shows the admin.

    A miss is ordinary, not exceptional: someone who has never opened the app
    has no row here at all. Nothing is created to paper over it, because a
    ``users`` row without a Seerr user id could never be matched to the same
    person again when they do sign in.
    """
    candidates = [name.lower() for name in (event.username, event.email) if name]
    if not candidates:
        return None

    rows = await db.execute(
        select(User).where(func.lower(User.plex_username).in_(candidates))
    )
    matches = list(rows.scalars().all())

    # Both names are fetched in one query and the preference applied here: SQL
    # would have to be told the order twice over, and there are never more than
    # a handful of rows to look at.
    for candidate in candidates:
        for user in matches:
            if user.plex_username.lower() == candidate:
                return user
    return None


def _media_of(event: SeerrWebhookEvent) -> MediaSummary:
    """What the notification's first line says.

    Seerr's ``subject`` is already ``Title (Year)`` and is the best name anyone
    in this exchange has — this service has no TMDB lookup on this path and
    nothing cached to consult. Split rather than passed through whole so the
    line is assembled the same way every other notification's is.

    The TMDB id is the same last resort ``POST /request`` falls back to, for the
    same reason: ugly, and more use than the word "Unknown".
    """
    if event.subject:
        title, year = split_year(event.subject)
        return MediaSummary(title=title, year=year)
    if event.tmdb_id is not None:
        return MediaSummary(title=f"TMDB {event.tmdb_id}")
    return MediaSummary(title="A new request")


def _notification_data(event: SeerrWebhookEvent) -> dict[str, Any]:
    """The structured facts that ride along with the two display lines.

    ``source`` is here so a client can tell a request it filed itself from one
    that appeared in Seerr, and so the two are distinguishable in a log without
    re-reading the text.
    """
    data: dict[str, Any] = {"source": "seerr"}
    if event.request_id is not None:
        data["seerr_request_id"] = event.request_id
    if event.tmdb_id is not None:
        data["tmdb_id"] = event.tmdb_id
    if event.media_type:
        data["media_type"] = event.media_type
    return data


def _log_detail(event: SeerrWebhookEvent, *, matched: bool) -> dict[str, Any]:
    """The ``activity_log`` row's detail.

    Deliberately the same shape ``POST /request`` writes — ``kind: "request"``,
    a TMDB id and a media type — so the activity page renders both without
    knowing there are two ways to file one. ``source`` is what tells them apart,
    and ``requested_by`` is what keeps an unmatched request readable: the row's
    ``user_id`` is null, so without it the page could only say "—".

    Keys whose values are unknown are left out rather than stored as null: the
    page reads this with ``.get(key, "?")``, and a stored null would print as
    the word ``None``.
    """
    detail: dict[str, Any] = {"kind": "request", "source": "seerr", "success": True}
    if event.request_id is not None:
        detail["seerr_request_id"] = event.request_id
    if event.tmdb_id is not None:
        detail["tmdb_id"] = event.tmdb_id
    if event.media_type:
        detail["type"] = event.media_type
    if event.username:
        detail["requested_by"] = event.username
    if not matched:
        detail["unmatched_user"] = True
    detail["seerr_event"] = event.notification_type
    return detail
