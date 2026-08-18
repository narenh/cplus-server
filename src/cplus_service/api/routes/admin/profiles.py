"""Quality profile CRUD and the rule builder.

The builder holds no server-side draft. Add / remove / move each post the whole
current form, and the server decodes it, applies the operation and re-renders
the rows. See :mod:`.rules_form`.

Rules are validated by stage 1's pydantic schema before saving, so the stored
JSON can never contain a shape the engine will not accept.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ....db.models import Action, QualityProfile
from ....quality.models import QualityProfile as ProfileSchema
from ....web import templates
from ...deps import DbDep
from .deps import AdminPageDep
from .rules_form import RULE_SPECS, SPECS_BY_TYPE, apply_op, decode_rules, encode_rule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quality-profiles", tags=["admin"])


def _rows_context(request: Request, rules: list[dict]) -> dict:
    return {
        "request": request,
        "rules": [encode_rule(rule) for rule in rules],
        "specs_by_type": SPECS_BY_TYPE,
        "rule_specs": RULE_SPECS,
    }


async def _load(db: DbDep, profile_id: int) -> QualityProfile:
    profile = await db.get(QualityProfile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such quality profile")
    return profile


@router.get("", response_class=HTMLResponse)
async def list_profiles(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    result = await db.execute(select(QualityProfile).order_by(QualityProfile.name))
    profiles = list(result.scalars().all())

    used = await db.execute(select(Action.quality_profile_id))
    in_use = {row for row in used.scalars().all() if row is not None}

    return templates.TemplateResponse(
        request,
        "profiles_list.html",
        {
            "profiles": profiles,
            "in_use": in_use,
            "admin": admin,
            "title": "Quality profiles",
            "nav": "profiles",
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_profile(request: Request, admin: AdminPageDep) -> Response:
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": None,
            "name": "",
            "rules": [],
            "rule_specs": RULE_SPECS,
            "specs_by_type": SPECS_BY_TYPE,
            "admin": admin,
            "title": "New quality profile",
            "nav": "profiles",
        },
    )


@router.get("/{profile_id}", response_class=HTMLResponse)
async def edit_profile(
    request: Request, db: DbDep, admin: AdminPageDep, profile_id: int
) -> Response:
    profile = await _load(db, profile_id)
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": profile,
            "name": profile.name,
            "rules": [encode_rule(rule) for rule in profile.rules or []],
            "rule_specs": RULE_SPECS,
            "specs_by_type": SPECS_BY_TYPE,
            "admin": admin,
            "title": f"Edit {profile.name}",
            "nav": "profiles",
        },
    )


@router.post("/rows", response_class=HTMLResponse)
async def rebuild_rows(request: Request, admin: AdminPageDep) -> Response:
    """Re-render the rule rows after an add / remove / move.

    Stateless: the posted form *is* the draft.
    """
    form = dict(await request.form())
    op = str(form.get("op", ""))
    rule_type = str(form.get("rule_type", "")) or None
    try:
        index = int(str(form.get("index", "-1")))
    except ValueError:
        index = -1

    rules = apply_op(decode_rules(form), op, index, rule_type)
    return templates.TemplateResponse(
        request, "partials/rule_rows.html", _rows_context(request, rules)
    )


@router.post("", response_class=HTMLResponse)
async def save_profile(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    """Create or update a profile. ``profile_id`` in the form decides which."""
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    raw_id = str(form.get("profile_id", "")).strip()
    rules = decode_rules(form)

    errors: list[str] = []
    if not name:
        errors.append("A profile needs a name.")

    try:
        # Stage 1 owns what a valid rule list is; do not second-guess it here.
        validated = ProfileSchema(name=name or "unnamed", rules=rules)
    except ValidationError as exc:
        validated = None
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"] if part != "rules")
            errors.append(f"{location or 'rules'}: {error['msg']}")

    if errors or validated is None:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile": None if not raw_id else await _load(db, int(raw_id)),
                "name": name,
                "rules": [encode_rule(rule) for rule in rules],
                "rule_specs": RULE_SPECS,
                "specs_by_type": SPECS_BY_TYPE,
                "errors": errors,
                "admin": admin,
                "title": "Quality profile",
                "nav": "profiles",
            },
            status_code=422,
        )

    stored = [rule.model_dump(mode="json") for rule in validated.rules]

    if raw_id:
        profile = await _load(db, int(raw_id))
        profile.name = name
        profile.rules = stored
    else:
        profile = QualityProfile(name=name, rules=stored)
        db.add(profile)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A quality profile named '{name}' already exists."
        ) from exc

    return RedirectResponse("/admin/quality-profiles", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{profile_id}/delete")
async def delete_profile(
    db: DbDep, admin: AdminPageDep, profile_id: int, confirm: str = Form(default="")
) -> Response:
    profile = await _load(db, profile_id)

    in_use = await db.execute(
        select(Action).where(Action.quality_profile_id == profile_id)
    )
    blocking = in_use.scalars().first()
    if blocking is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{profile.name}' is still used by the action '{blocking.name}'.",
        )

    await db.delete(profile)
    return RedirectResponse("/admin/quality-profiles", status_code=status.HTTP_303_SEE_OTHER)
