"""``GET /capabilities`` — what this instance can do, before anyone logs in.

The app has to know whether to ask for notification permission, and that is not
a fact about the user: it is a fact about the instance they are pointed at. An
admin who enables notifications six months after their users installed the app
should not need every one of them to sign out and back in for it to take
effect.

So this endpoint exists, and **registration is driven by (OS permission ×
capability flag), never by login events.** The app checks here on launch and on
foreground; when ``notifications`` flips to true it prompts for permission if it
has not already, and registers the token it gets. Login only decides *which*
instance's ``POST /manager/push-devices`` it would call once both conditions
hold.

Unauthenticated, deliberately. It reports no user-specific fact, no
configuration value and no secret — only that a feature is switched on, which
anyone who can reach the login page can infer anyway. Requiring a token would
mean the app could not check it before sign-in, which is the case that motivated
the endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...db.session import get_config
from ..deps import DbDep

router = APIRouter(tags=["client"])


@router.get("/capabilities")
async def capabilities(db: DbDep) -> dict[str, bool]:
    """What this instance has switched on.

    ``notifications`` is the master toggle *only*. It is deliberately not "and
    a relay key is set and a device could register": an admin mid-setup would
    see it flap, and the app has nothing useful to do differently in that
    window. A registration attempt against an instance that is enabled but
    unconfigured is refused with a message saying so, which is where that
    distinction belongs.

    A dict rather than a model with fixed fields, so a later version can add a
    capability without a client that has never heard of it seeing a shape it
    does not recognise.
    """
    config = await get_config(db)
    return {"notifications": bool(config.notifications_enabled)}
