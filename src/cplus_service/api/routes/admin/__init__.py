"""Admin webui — session-authenticated, gated on Seerr's ADMIN bit.

Route layout:

* ``login`` is deliberately ungated; everything else requires a session.
* ``config`` also carries the Prowlarr proxy endpoints, which answer JSON by
  default and HTML with ``?format=html``.
* the rest are ordinary CRUD pages rendered with Jinja2 + HTMX.
"""

from fastapi import APIRouter

from . import actions, activity, config, login, notifications, permissions, profiles

router = APIRouter(prefix="/admin")

router.include_router(login.router)
router.include_router(config.router)
router.include_router(profiles.router)
router.include_router(actions.router)
router.include_router(permissions.router)
router.include_router(activity.router)
router.include_router(notifications.router)

__all__ = ["router"]
