"""Admin route skeleton — stage 3 fills these in.

Every handler here returns 501. They exist now purely so the route structure and
paths are settled before the webui is written, and so stage 3 is a matter of
implementing bodies rather than designing a URL space.

Deliberately left unauthenticated for now: :func:`cplus_service.api.deps.get_admin`
is written and tested, and wiring it in is part of implementing these. A stub
that returns 501 to everyone leaks nothing.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/admin", tags=["admin (stage 3)"])


def _todo(what: str) -> NoReturn:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"{what} arrives in stage 3")


@router.get("/config")
async def get_config_route() -> Any:
    _todo("Reading configuration")


@router.post("/config")
async def update_config_route() -> Any:
    _todo("Updating configuration")


@router.post("/config/verify-prowlarr")
async def verify_prowlarr_route() -> Any:
    _todo("Verifying the Prowlarr connection")


@router.get("/prowlarr/download-clients")
async def list_download_clients_route() -> Any:
    _todo("Listing Prowlarr download clients")


@router.get("/prowlarr/indexers")
async def list_indexers_route() -> Any:
    _todo("Listing Prowlarr indexers")


@router.get("/quality-profiles")
async def list_quality_profiles_route() -> Any:
    _todo("Listing quality profiles")


@router.post("/quality-profiles")
async def create_quality_profile_route() -> Any:
    _todo("Creating a quality profile")


@router.put("/quality-profiles/{profile_id}")
async def update_quality_profile_route(profile_id: int) -> Any:
    _todo("Updating a quality profile")


@router.delete("/quality-profiles/{profile_id}")
async def delete_quality_profile_route(profile_id: int) -> Any:
    _todo("Deleting a quality profile")


@router.get("/actions")
async def list_actions_route() -> Any:
    _todo("Listing actions")


@router.post("/actions")
async def create_action_route() -> Any:
    _todo("Creating an action")


@router.put("/actions/{action_id}")
async def update_action_route(action_id: int) -> Any:
    _todo("Updating an action")


@router.delete("/actions/{action_id}")
async def delete_action_route(action_id: int) -> Any:
    _todo("Deleting an action")


@router.get("/users")
async def list_users_route() -> Any:
    _todo("Listing users")


@router.get("/users/{user_id}/permissions")
async def get_permissions_route(user_id: int) -> Any:
    _todo("Reading a user's permissions")


@router.put("/users/{user_id}/permissions")
async def set_permissions_route(user_id: int) -> Any:
    _todo("Setting a user's permissions")


@router.get("/grabs")
async def list_grabs_route() -> Any:
    _todo("Listing grabs")


@router.get("/activity-log")
async def list_activity_log_route() -> Any:
    _todo("Listing the activity log")
