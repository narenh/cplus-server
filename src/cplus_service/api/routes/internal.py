"""Routes only the aligner sidecar calls.

``GET /internal/aligner/jobs/{id}/tracks/{n}`` relays one of a job's audio files
from Plex, Range requests included — the sidecar seeks through it for
verification samples and resumes interrupted downloads. The admin's Plex token
is added here and never leaves this container.

Authorised by the job's own random key (``X-Aligner-Key``), which exists only in
the job's row and in its ``job.json`` on the shared volume, and which stops
working the moment the job is no longer active. A wrong key, a finished job and
a job that never existed all look the same from outside: 404.
"""

from __future__ import annotations

import hmac
from typing import Annotated

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ...db.models import AudiobookJob
from ...db.session import get_config
from ...plex.client import PlexServerClient
from ..deps import StateDep

router = APIRouter(prefix="/internal/aligner", include_in_schema=False)

#: Response headers passed through from Plex. Everything a range-aware reader
#: needs, and nothing that identifies the server.
PASS_HEADERS = ("content-length", "content-range", "accept-ranges", "content-type")


@router.get("/jobs/{job_id}/tracks/{n}")
async def track_audio(
    job_id: int,
    n: int,
    request: Request,
    state: StateDep,
    x_aligner_key: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    # A session of its own, closed before the first byte streams: a request
    # dependency's session would stay open for the whole transfer, holding a
    # read transaction for minutes.
    async with state.sessionmaker() as db:
        job = await db.get(AudiobookJob, job_id)
        config = await get_config(db)
        base, token = config.plex_server_base_url, config.plex_admin_token

    if (
        job is None
        or not x_aligner_key
        or not hmac.compare_digest(job.secret.encode(), x_aligner_key.encode())
        or not job.is_active
        or not 0 <= n < len(job.tracks)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not base or not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Not connected to Plex")

    plex = PlexServerClient(base, token, client=state.http)
    headers = {}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        upstream = await state.http.send(
            plex.open_stream(job.tracks[n]["key"], headers=headers), stream=True
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Plex didn't answer") from exc
    if upstream.status_code >= 400:
        await upstream.aclose()
        code = upstream.status_code
        raise HTTPException(
            code if code == 416 else status.HTTP_502_BAD_GATEWAY, f"Plex answered {code}"
        )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={k: upstream.headers[k] for k in PASS_HEADERS if k in upstream.headers},
        background=BackgroundTask(upstream.aclose),
    )
