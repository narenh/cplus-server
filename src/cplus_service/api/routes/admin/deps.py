"""Admin-only request dependencies.

The public API answers an unauthenticated caller with 401 JSON, which is right
for tvOS but useless in a browser. This wrapper sends a signed-out admin to the
login page instead, in whichever way the request can act on:

* an HTMX request gets ``HX-Redirect``, which HTMX obeys by navigating — a bare
  303 would otherwise be followed transparently and the login page swapped into
  whatever fragment triggered it;
* a normal navigation gets a 303.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from ....auth.sessions import SESSION_COOKIE_NAME, resolve_session
from ....db.models import User
from ...deps import DbDep

LOGIN_PATH = "/admin/login"


async def require_admin_page(
    request: Request,
    db: DbDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> User:
    """Resolve the signed-in admin, or redirect to the login page."""
    user = await resolve_session(db, session_cookie)
    if user is not None:
        return user

    if request.headers.get("HX-Request"):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session expired",
            headers={"HX-Redirect": LOGIN_PATH},
        )
    raise HTTPException(
        status.HTTP_303_SEE_OTHER, "Not signed in", headers={"Location": LOGIN_PATH}
    )


AdminPageDep = Annotated[User, Depends(require_admin_page)]
