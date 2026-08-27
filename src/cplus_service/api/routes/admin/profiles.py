"""Quality profile CRUD, the builder, and the preview.

The builder holds no server-side draft. Add / remove / move each post the whole
current form, and the server decodes it, applies the operation and re-renders.
See :mod:`.rules_form`.

Rules and choices are validated by stage 1's pydantic schema before saving, so
the stored JSON can never contain a shape the engine will not accept.

The preview is the reason the builder is worth using. It ranks a candidate set
through the *unsaved* draft and shows what happened to every release — the
position it landed in, the choice it matched, or the filter that dropped it.
Two sources: a fixed sample cast that needs nothing configured (see
:mod:`cplus_service.quality.samples`), and a real Prowlarr search when an admin
wants to check the profile against a title they actually care about.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ....db.models import Action, QualityProfile
from ....db.session import get_config
from ....prowlarr.client import ProwlarrClient, ProwlarrError
from ....quality.describe import ordinal, summarise
from ....quality.engine import explain
from ....quality.models import QualityProfile as ProfileSchema
from ....quality.samples import sample_releases
from ....release.models import ParsedRelease
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep
from .rules_form import (
    AUDIO_OPTIONS,
    CHOICE_FIELDS,
    HDR_OPTIONS,
    RESOLUTION_OPTIONS,
    RULE_SPECS,
    SOURCE_OPTIONS,
    SPECS_BY_TYPE,
    TIE_BREAK_OPTIONS,
    apply_choice_op,
    apply_rule_op,
    decode_choices,
    decode_rules,
    encode_choices,
    encode_rules,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quality-profiles", tags=["admin"])

#: A preview against a real search needs an IMDB id or a query. Anything
#: shaped like ``tt1234567`` is treated as the former.
IMDB_PREFIX = "tt"


def _draft(rules: list[dict], choices: list[dict]) -> ProfileSchema | None:
    """The draft as the engine would see it, or ``None`` if it is not valid yet.

    The preview is drawn while an admin is halfway through typing, so an
    invalid draft is an ordinary state here, not an error to report.
    """
    try:
        return ProfileSchema(name="draft", rules=rules, choices=choices)
    except ValidationError:
        return None


def _builder_context(rules: list[dict], choices: list[dict]) -> dict:
    """Everything both the full page and the re-rendered builder need."""
    draft = _draft(rules, choices)
    summary = summarise(draft) if draft is not None else None
    return {
        "rules": encode_rules(rules),
        "choices": encode_choices(choices),
        "rule_specs": RULE_SPECS,
        "specs_by_type": SPECS_BY_TYPE,
        "choice_fields": CHOICE_FIELDS,
        "tie_break_options": TIE_BREAK_OPTIONS,
        "resolution_options": RESOLUTION_OPTIONS,
        "source_options": SOURCE_OPTIONS,
        "hdr_options": HDR_OPTIONS,
        "audio_options": AUDIO_OPTIONS,
        "summary": summary,
        # Each choice's own reading, so a row can say what it means without the
        # admin having to look back up at the summary.
        "readings": summary.choices if summary is not None else [""] * len(choices),
        "ordinal": ordinal,
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
            "summaries": {
                profile.id: summarise(
                    ProfileSchema(
                        id=profile.id,
                        name=profile.name,
                        rules=profile.rules or [],
                        choices=profile.choices or [],
                    )
                )
                for profile in profiles
            },
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
            **_builder_context([], []),
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
            **_builder_context(list(profile.rules or []), list(profile.choices or [])),
            "admin": admin,
            "title": f"Edit {profile.name}",
            "nav": "profiles",
        },
    )


@router.post("/rows", response_class=HTMLResponse)
async def rebuild_rows(request: Request, admin: AdminPageDep) -> Response:
    """Re-render the builder after an add / remove / move.

    Stateless: the posted form *is* the draft. ``target`` says which list the
    operation is against — the two sections are edited independently, but both
    are re-rendered, since the summary spans them.
    """
    form = await request.form()
    op = str(form.get("op", ""))
    target = str(form.get("target", "rules"))
    # Each section has its own add-dropdown, distinctly named: one shared name
    # would make the tie-breaker button add whatever the filter dropdown showed.
    kind = str(form.get("kind", "filter"))
    rule_type = str(form.get(f"rule_type_{kind}", "")) or None
    try:
        index = int(str(form.get("index", "-1")))
    except ValueError:
        index = -1

    rules = decode_rules(form)
    choices = decode_choices(form)

    if target == "choices":
        choices = apply_choice_op(choices, op, index)
    else:
        rules = apply_rule_op(rules, op, index, rule_type)

    return templates.TemplateResponse(
        request, "partials/builder.html", {"request": request, **_builder_context(rules, choices)}
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    """Rank a candidate set through the unsaved draft and show the workings.

    Answers on the draft in the form, never on what is stored: the question an
    admin has while editing is what *this* version of the rules would do.
    """
    form = await request.form()
    draft = _draft(decode_rules(form), decode_choices(form))
    query = str(form.get("preview_query", "")).strip()
    live = str(form.get("preview_source", "")) == "prowlarr"

    candidates: list[ParsedRelease] = []
    error: str | None = None
    if live:
        candidates, error = await _live_candidates(db, state, query)
    else:
        candidates = sample_releases()

    return templates.TemplateResponse(
        request,
        "partials/preview.html",
        {
            "request": request,
            "judgements": explain(candidates, draft) if draft is not None else [],
            "invalid": draft is None,
            "live": live,
            "query": query,
            "error": error,
            "summary": summarise(draft) if draft is not None else None,
            "ordinal": ordinal,
        },
    )


async def _live_candidates(
    db: DbDep, state: StateDep, query: str
) -> tuple[list[ParsedRelease], str | None]:
    """Real Prowlarr results for the preview, or an empty list and a reason.

    Every failure here is reported as text in the panel rather than as a status
    code: the preview is a side panel on a page the admin is editing, and
    losing their draft to a failed lookup would be a worse answer than "that
    didn't work".
    """
    if not query:
        return [], "Type a title or an IMDB id to search for."

    config = await get_config(db)
    if not config.prowlarr_url or not config.prowlarr_api_key:
        return [], "Prowlarr is not configured yet — the sample releases still work."

    prowlarr = ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key, client=state.http)
    try:
        if query.lower().startswith(IMDB_PREFIX) and query[2:].isdigit():
            return await prowlarr.search_movie(query), None
        return await prowlarr.search_query(query), None
    except ProwlarrError as exc:
        return [], str(exc)


@router.post("", response_class=HTMLResponse)
async def save_profile(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    """Create or update a profile. ``profile_id`` in the form decides which."""
    form = await request.form()
    name = str(form.get("name", "")).strip()
    raw_id = str(form.get("profile_id", "")).strip()
    rules = decode_rules(form)
    choices = decode_choices(form)

    errors: list[str] = []
    if not name:
        errors.append("A profile needs a name.")

    try:
        # Stage 1 owns what a valid rule list is; do not second-guess it here.
        validated = ProfileSchema(name=name or "unnamed", rules=rules, choices=choices)
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
                **_builder_context(rules, choices),
                "errors": errors,
                "admin": admin,
                "title": "Quality profile",
                "nav": "profiles",
            },
            status_code=422,
        )

    stored_rules = [rule.model_dump(mode="json") for rule in validated.rules]
    stored_choices = [choice.model_dump(mode="json") for choice in validated.choices]

    if raw_id:
        profile = await _load(db, int(raw_id))
        profile.name = name
        profile.rules = stored_rules
        profile.choices = stored_choices
    else:
        profile = QualityProfile(name=name, rules=stored_rules, choices=stored_choices)
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
async def delete_profile(db: DbDep, admin: AdminPageDep, profile_id: int) -> Response:
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
