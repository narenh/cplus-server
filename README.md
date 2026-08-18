# cplus-server

Backend self-hosted service for integrating Canopy+ with Seerr/Prowlarr.

`cplus-service` lets a Plex/Seerr-owning homelab admin expose a curated,
permissioned subset of Prowlarr search/grab functionality to their Seerr users —
without those users ever seeing the Prowlarr API key. It is consumed by the
Canopy+ tvOS client but is built as a generic service any Seerr admin can run.

**Out of scope, permanently:** Sonarr and Radarr. This service talks to Prowlarr
(search, grab, indexers, download clients) and Seerr (auth, plus a built-in
Request action) and nothing else. No library sync. The Prowlarr-backed side is
movies-only and driven by IMDB ID — no free-text search, so no title-matching
ambiguity to resolve.

---

## Build status

This repository currently contains **stages 1 and 2 of 3**.

| | Component | Status |
|---|---|---|
| 1 | Data model + migrations | ✅ done |
| 1 | Release parser | ✅ done |
| 1 | Prowlarr client wrapper | ✅ done |
| 1 | Quality profile rule engine | ✅ done |
| 2 | Seerr client + both auth flows | ✅ done |
| 2 | `/actions`, `/search`, `/grab`, `/request` | ✅ done |
| 2 | Built-in Request action | ✅ done |
| 3 | Admin web UI | routes stubbed (501) |
| 3 | Docker packaging | not started |

---

## Getting started

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

pytest                      # 285 tests; no network, Prowlarr or Seerr needed
ruff check .
```

Run the service:

```bash
export CPLUS_DB_PATH=./cplus.db      # optional; defaults to ./cplus.db
alembic upgrade head
python -m cplus_service              # CPLUS_HOST / CPLUS_PORT to override
```

Exercise the stage-1 modules standalone, with no server:

```bash
python scripts/demo.py               # canned release set, recommend per profile

export PROWLARR_URL=http://prowlarr.local:9696
export PROWLARR_API_KEY=...
export PREFERRED_INDEXER_ID=3        # optional; unset means "All indexers"
python scripts/demo.py tt1160419     # against a real Prowlarr
```

---

## Layout

```
src/cplus_service/
  release/models.py     ParsedTitle / ParsedRelease — the stable client contract
  release/parser.py     title -> structured metadata; drops full discs
  quality/models.py     quality profile rule schema (pydantic, discriminated union)
  quality/engine.py     recommend(candidates, profile) -> ParsedRelease | None
  prowlarr/client.py    async Prowlarr API wrapper
  seerr/client.py       async Seerr API wrapper (auth + request creation only)
  auth/plex_cache.py    tvOS Plex-token -> user cache
  auth/sessions.py      webui browser sessions
  auth/identity.py      Seerr user -> local user upsert
  search/stream.py      two-phase concurrent search, NDJSON phases
  api/app.py            FastAPI factory + lifespan
  api/deps.py           auth/config/client dependencies
  api/routes/           actions, search, grab, request, auth, admin (stubs)
  db/models.py          SQLAlchemy 2.0 schema
  bootstrap.py          seeds the built-in Request action
migrations/             Alembic
scripts/demo.py         offline + live REPL-style driver
tests/                  unit + ASGI end-to-end tests
```

---

## Release parser

`parse_title(title) -> ParsedTitle` extracts structured metadata from a release
name. `parse_prowlarr_results(raws) -> list[ParsedRelease]` does the same across
a Prowlarr search payload and **drops full discs entirely**.

### Delimiter normalisation

Scene names are dot-delimited (`Movie.2024.2160p.HDR.DTS-HD.MA-GROUP`), many
indexers hand back the same name space-delimited, and real titles mix both.
Every pattern runs against a normalised form where `.` and `_` are folded to
spaces, so one pattern matches all three shapes. Token boundaries use explicit
lookarounds rather than `\b`, so a hyphen counts as a boundary but an
alphanumeric does not.

This is the specific bug the Swift implementation had — it matched on spaces
only and silently missed most real-world titles. Every field in the test suite
is covered with dot-delimited, space-delimited and mixed examples. **If you add
a pattern here, add a dot-delimited test for it.**

### Fields

| Field | Notes |
|---|---|
| `title` | raw, unmodified |
| `resolution` | `2160p` / `1080p` / `720p` / `480p` / `unknown` |
| `source` | `WEB-DL` / `WEBRip` / `BluRay` / `REMUX` / `encode` / `unknown` |
| `dv_profile` | `0` = no Dolby Vision, otherwise best-effort profile number |
| `is_hdr10plus` | HDR10+ |
| `is_hdr` | plain HDR10 — mutually exclusive with `is_hdr10plus` |
| `has_atmos`, `has_dtsx`, `has_truehd` | independent; a release may carry all three |
| `is_repack_or_proper` + `repack_version` | `REPACK2` → 2, `REAL.PROPER` → 2 |
| `is_prerelease` | CAM / CAMRip / HDCAM / TS / HDTS / telesync / TC / HDTC / telecine / HDRip / screener / DVDSCR / R5 / workprint / DCP / DCPRip |
| `is_full_disc` | always `False` on anything a caller receives |
| `release_group` | best-effort trailing `-GROUP` |
| `base_title` | normalised name with tags and group stripped, for repack title-diffing |
| `hdr_tags`, `audio_tags` | computed canonical tokens (see below) |

Plus Prowlarr passthrough, so the tvOS client can section and sort without a
second round trip: `guid`, `indexer_id`, `indexer`, `size_bytes`,
`publish_date`, `seeders`, `leechers`, `download_url`, `info_url`, `protocol`.

### Dolby Vision

Explicit markers in the title always win — `DV.P8`, `DVHE.05`,
`DoVi.Profile.8`, and `FEL`/`MEL` (both the dual-layer profile 7). Only when
nothing explicit is present does it fall back to the heuristic:

| Situation | Inferred profile |
|---|---|
| REMUX + DV | 7 |
| encode + DV | 8 |
| WEB + DV + HDR | 8 |
| WEB + DV only | 5 |

### Full discs

A release is a full disc when it is not WEB, not REMUX and carries no evidence
of re-encoding — either explicitly marked (`BDMV`, `COMPLETE.BLURAY`, `BD66`,
`ISO`, `UNTOUCHED`) or naming a disc-only codec (AVC, VC-1, MPEG-2) alongside a
BluRay token.

Encode evidence is x264 / x265 / **x266** / **HEVC** / **AV1** / XviD / DivX and
the `*Rip` family — not just x264/x265.

One documented conservatism trade-off: a BluRay-tagged title carrying no codec
token at all (`...1080p.BluRay.DTS-HD.MA.5.1-GROUP`) is read as a full disc,
because "not WEB, not REMUX, not encode" is the definition. In practice
disc-sourced encodes essentially always name their codec.

### No categorisation, anywhere

The parser returns a **flat, tagged, full-disc-free list in Prowlarr's own
order**. There is no sorting, bucketing or `category` field in this service, by
design — sectioning is a tvOS client-side concern driven purely by these tags
plus `size_bytes` / `publish_date`. Do not add one.

---

## Quality profile rule engine

```python
recommend(candidates: list[ParsedRelease], profile: QualityProfile) -> ParsedRelease | None
```

Pure: no I/O, no database, no clock. `rank()` is also exported for a "why this
release?" view. `None` is an expected, valid outcome — it means every candidate
was eliminated by the filters. It is not an error.

A profile is an **ordered list of rules**, with both kinds coexisting in the one
list.

**Filter rules** — eliminate candidates before ranking. Position in the list is
irrelevant.

| Rule | Behaviour |
|---|---|
| `exclude_prerelease` | drops pre-release candidates. Off by default (absent from the profile) |
| `keyword_exclude` | drops releases whose raw title contains any of these, case-insensitively |
| `size_cap_gb` | drops releases larger than the cap. Unknown size is kept — it is not evidence of a violation |

**Preference rules** — rank the survivors. Position **is** load-bearing: rules
apply in the order they appear in the profile, each breaking the ties left by
the previous. The conventional ordering (available as `default_profile()`) is:

1. `repack_proper_priority` — prefers a REPACK/PROPER over the base release of the same underlying title. Title-diffed via `base_title`, not tag-matched: a REPACK of *another* movie never demotes an unrelated release.
2. `resolution_order` — e.g. `["2160p", "1080p"]`
3. `source_order` — e.g. `["WEB-DL", "WEBRip", "BluRay", "REMUX"]`
4. `hdr_match` — ordered HDR/DV tokens
5. `audio_match` — ordered audio tokens
6. `size` — final tie-break

Values not named in an ordered rule rank last but are **not** filtered out.

### Token vocabulary

`hdr_match` accepts `DV`, `DV_P5` / `DV_P7` / `DV_P8` (any `DV_P<n>`), `HDR10+`,
`HDR10`, `SDR`. A release is scored by the best-ranked token it carries, so a DV
profile 8 release matches both the precise `DV_P8` and the coarse `DV`. `SDR` is
emitted only when a release has no DV, no HDR10+ and no HDR10.

`audio_match` accepts `Atmos`, `DTS:X`, `TrueHD` as distinct values.

### The two size rules are different things

| | `size_cap_gb` (filter) | `size` (preference) |
|---|---|---|
| Effect | eliminates outright | reorders only |
| Over-cap release | can never be recommended | still wins if it is all that is left |
| `cap_gb` on the preference rule | — | demotes over-cap behind under-cap; among over-cap, smallest (closest to the cap) wins |

Keep these separate. Both involve a GB number and they are not interchangeable.

### The preferred-indexer filter is not a profile rule

`config.preferred_indexer_id` applies **unconditionally to every profile**, once,
before any profile's own rules run. There is no per-profile toggle.

* `None` ("All indexers") — no restriction.
* Set, and that indexer returned something — restrict to it.
* Set, but that indexer returned nothing — **fall back to the full set**, rather
  than returning no recommendation.

The engine does not do this itself: it takes the effective candidate set as an
argument, because the orchestration belongs in stage 2's search endpoint.
`preferred_indexer_candidates(candidates, preferred_indexer_id)` implements the
rule for the caller to apply.

---

## Prowlarr client

Async from the start (`httpx`), because search latency is dominated by Prowlarr
fanning out to indexers and stage 2 will want concurrent searches. Nothing calls
it concurrently yet.

```python
async with ProwlarrClient(url, api_key) as prowlarr:
    await prowlarr.verify_connection()            # backs the admin Connect/Verify button
    await prowlarr.list_indexers()
    await prowlarr.list_download_clients()
    releases = await prowlarr.search_movie("tt0111161", indexer_ids=[3])
    await prowlarr.grab(guid=..., indexer_id=..., download_client_id=...)
```

`search_movie` results are already parsed, tagged and free of full discs —
callers of this wrapper never see a raw Prowlarr release dict. Transport errors
and non-2xx responses both surface as `ProwlarrError`, which carries
`status_code` (`None` when no response arrived).

---

## Data model

SQLite via SQLAlchemy 2.0 async + Alembic. Most tables are not populated until
stage 2; they exist now so the migration history has one starting point.

| Table | Contents |
|---|---|
| `config` | singleton row (CHECK-enforced): `seerr_url`, `prowlarr_url`, `prowlarr_api_key`, `preferred_indexer_id` |
| `users` | `seerr_user_id` (unique), `plex_username` |
| `quality_profiles` | `name`, `rules` (ordered JSON list) |
| `actions` | `name`, `download_client_id`, `quality_profile_id` |
| `permissions` | user ↔ action, composite PK |
| `grabs` | user, action, release title/guid/indexer/size, `created_at` |
| `activity_log` | user, `event_type` (`search`\|`grab`), `detail` JSON, `created_at` |

`PRAGMA foreign_keys=ON` is set per connection — SQLite defaults it *off*, which
would silently ignore every `ON DELETE` clause. Deleting a user cascades to
permissions; deleting an action nulls the reference but keeps the grab history;
a quality profile in use by an action cannot be deleted.

`cplus_service.db.QualityProfile` (ORM row) and
`cplus_service.quality.QualityProfile` (pydantic rule schema) share a name.
`ProfileSchema(id=row.id, name=row.name, rules=row.rules)` converts one to the
other.

---

## Auth

Two flows that must not be conflated. tvOS already holds a user-scoped Plex
token; a browser does not.

### tvOS — Plex token on every request

No login step, no session token. The header is `X-Plex-Token`.

1. `GET /actions` on app launch (and on reconnect in settings) validates the
   token against Seerr's `/api/v1/auth/plex`, upserts the local `users` row, and
   caches the token → user mapping.
2. `/search` and `/grab` authenticate **against that cache only** — no outbound
   Plex or Seerr call, which is what keeps them fast.
3. A cache miss (server restarted) is a `401`; the client's recovery is to call
   `/actions` again, which happens on next launch anyway.
4. `/request` is the exception: it always validates live, because it needs a
   Seerr session to file the request as the user.

The cache has **no TTL and no persistence**, deliberately. An entry is valid
until that user's next `/actions` call overwrites it. Tokens are keyed by
SHA-256, never stored raw.

Revoking a user's permissions therefore takes effect at their next `/actions`
call, not immediately. That is the accepted tradeoff for cache-only search.

### Webui — Plex OAuth PIN flow + browser session

1. The browser runs the standard Plex PIN flow against plex.tv. This service
   takes no part in it and only ever sees the finished token.
2. `POST /auth` with `{plex_token, seerr_url?}`.
3. The token is validated against Seerr, and Seerr's **ADMIN permission bit**
   (`permissions & 2`) is checked — not `seerr_user_id == 1`, since Seerr grants
   admin through the bitmask and the owner is not guaranteed to be user 1.
4. A non-admin is rejected outright: the webui has no non-admin use case.
5. An admin gets an opaque session token in an httpOnly `cplus_session` cookie,
   backed by the `admin_sessions` table.

`seerr_url` is accepted in the body to make first-run bootstrap possible —
setting it needs an admin session, and getting a session needs it. It is
persisted only after it has been proven to work, so a typo cannot brick config.

---

## Endpoints

### Client (tvOS)

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /actions` | live Seerr | The auth checkpoint. Returns `{id, name}` only |
| `GET /search?imdb_id=&type=movie` | cache | NDJSON stream, see below |
| `POST /grab` | cache | `{action_id, release_guid, indexer_id, release_title, size_bytes?}` |
| `POST /request` | live Seerr | `{tmdb_id, type, seasons?}` |

`/actions` returns just an id and a label — the client has no use for the
download client or quality profile behind an action. It routes on the name:
`"Request"` posts to `/request`, everything else to `/grab`. That is safe
because the Request action is a **system action** and cannot be renamed or
deleted.

`POST /grab` echoes back the release fields the client already received in the
search stream. `indexer_id` is what Prowlarr needs to identify the listing;
`release_title` and `size_bytes` are stored on the `grabs` row so the admin
UI's history is readable without re-querying an indexer for a listing that may
no longer exist. `size_bytes` is optional, because not every indexer reports one
— an unknown size is a real state rather than a client omission.

The client never supplies `download_client_id`: that comes from the action, and
the body rejects unknown fields.

Because the grab body is self-contained, the server keeps **no state between
search and grab** — a restart between the two is harmless.

### Webui

`POST /auth`, `POST /auth/logout`.

### Admin

`/admin/*` is stubbed at `501` — the URL space is settled so stage 3 fills in
bodies rather than designing routes. `deps.get_admin` is written and tested,
ready to be wired in.

---

## Streaming search

Both Prowlarr calls are issued **concurrently**:

1. scoped to `config.preferred_indexer_id` — **skipped entirely** when that is
   null, since there is nothing distinct to fetch early;
2. across all indexers.

The response is NDJSON, one object per line:

```
{"phase":"preferred","releases":[…],"recommendations":{"1":"guid-a","2":null}}
{"phase":"all","releases":[…],"recommendations":{"1":"guid-a","2":"guid-b"}}
```

**Client merge rule: apply the last line you received, wholesale.** Union the
`releases` arrays by guid.

That is the documented choice for the "re-send all vs. only the unresolved
ones" question: the `all` line always carries a recommendation for **every**
permitted action, so the client needs no key-level merging and behaves the same
whether or not phase 1 ran. The re-sent values never contradict phase 1 —
scoring goes through `preferred_indexer_candidates`, so a non-empty preferred
subset keeps its answer and an empty one falls back to the full set.

`releases` in the `all` line excludes anything already sent in `preferred`,
though clients should tolerate duplicates by guid regardless.

The `all` line is **always** sent — on zero results, and on Prowlarr failure —
so the client can always leave its loading state. Since the response has already
committed to `200` by then, a late failure appears in-band as an `error` field
rather than as a status code. A failure of the *preferred* call alone degrades
silently to a single phase, because the unfiltered search covers that indexer
too.

The built-in Request action is excluded from recommendations: it has no quality
profile and never touches Prowlarr.

---

## Built-in Request action

Seeded idempotently on startup, marked `is_system`, and carrying neither a
download client nor a quality profile — a CHECK constraint allows those nulls
only for a system action. Granted per user through the normal `permissions`
table like any other action, but not editable or deletable.

It is the one part of the service that is **not** movies-only, and the one that
is **TMDB-keyed** rather than IMDB-keyed, because that is what Seerr's request
endpoint takes. The client sends the TMDB id straight from Plex metadata; the
service never resolves IMDB → TMDB.

`seasons` is required and non-empty for `type=tv`, rejected for `type=movie`,
and passed through to Seerr verbatim. Season `0` is specials. We never
substitute the literal `"all"`, which would silently drop them.

No `grabs` row is written — nothing was grabbed. It is recorded in
`activity_log` instead, with `detail.kind == "request"`; the `event_type` enum
is `search | grab` per the stage-1 schema, so requests are logged as `grab` with
that discriminator rather than widening the enum.

---

## Notes for stage 3

* Admin routes are stubbed at `501` in `api/routes/admin.py`; wire
  `deps.AdminDep` in as you implement each.
* **Stage 3's admin UI must reserve the name `Request`** — the tvOS client
  routes on it. `bootstrap.REQUEST_ACTION_NAME` is the constant to check
  against, and system actions must be refused for edit and delete.
* `auth.plex_cache.forget_user` and `auth.sessions.destroy_sessions_for_user`
  exist so deleting a user can invalidate their access immediately rather than
  at their next launch.
* Seerr and Prowlarr use **separate** `httpx` clients (`state.http` vs
  `state.seerr_http`) and `SeerrClient` builds requests without consulting the
  cookie jar. Both guard against one user's Seerr session cookie being attached
  to another user's request, or leaking to Prowlarr. Keep them separate.
