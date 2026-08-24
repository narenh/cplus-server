# cplus-server

Backend self-hosted service for integrating Canopy+ with Seerr/Prowlarr.

`cplus-service` lets a Plex/Seerr-owning homelab admin expose a curated,
permissioned subset of Prowlarr search/grab functionality to their Seerr users —
without those users ever seeing the Prowlarr API key. It is consumed by the
Canopy+ tvOS client but is built as a generic service any Seerr admin can run.

**Out of scope, permanently:** Sonarr and Radarr. This service talks to Prowlarr
(search, grab, indexers, download clients) and Seerr (auth, plus an allowlisted
set of request operations) and nothing else. No library sync. The Prowlarr-backed side is
**movies-only** when driven by IMDB ID. Two things sit outside that: the
built-in **Request** action, which supports TV, is keyed by TMDB id and never
touches Prowlarr at all; and **free-text search**, which is not category-scoped
and returns whatever Prowlarr indexes.

---

## Build status

All three build stages are complete.

| | Component | Status |
|---|---|---|
| 1 | Data model + migrations | ✅ done |
| 1 | Release parser | ✅ done |
| 1 | Prowlarr client wrapper | ✅ done |
| 1 | Quality profile rule engine | ✅ done |
| 2 | Seerr client + both auth flows | ✅ done |
| 2 | `/actions`, `/search`, `/grab`, `/request` | ✅ done |
| 2 | Built-in Request action | ✅ done |
| 3 | Admin web UI (Jinja2 + HTMX) | ✅ done |
| 3 | Docker packaging | ✅ done |

---

## Running it

```bash
docker compose up -d          # then open http://localhost:8080
```

Locally, `docker-compose.override.yml` is merged automatically and is what
publishes the host port. Set `CPLUS_HOST_PORT` if 8080 is taken. Coolify never
applies that file — it invokes compose with an explicit `-f`, which disables
the automatic override merge.

That is the whole deployment. Everything else — Seerr URL, Prowlarr connection,
quality profiles, actions, permissions — is configured in the web UI, not in
environment variables or config files.

### Deploying with Coolify

1. **+ New Resource → Docker Compose**, pointed at this repository, branch
   `master`, compose file `docker-compose.yml`.
2. Assign a domain. Coolify substitutes it into `SERVICE_FQDN_CPLUS_8080` and
   handles the proxy and TLS certificate; nothing else needs configuring.
3. Deploy.

**Do not add a `ports:` mapping to `docker-compose.yml`.** Coolify's proxy
reaches the container over the Docker network, so `expose: 8080` is all it
needs. Publishing a host port there binds `0.0.0.0:8080` on the Coolify host,
which fails the deploy outright if anything already holds that port — and if it
succeeds, leaves the service reachable bypassing the proxy and its TLS. The
port lives in the magic variable's *name* (`SERVICE_FQDN_<NAME>_<PORT>`), not
in its value.

**Check the volume before you rely on it.** All state — config, users, quality
profiles, actions, permissions, grabs, sessions — is the single SQLite file at
`/data/cplus.db`. The compose file declares `cplus-data:/data` as a named
volume so redeploys keep it. If that ever becomes a non-persistent path, every
redeploy silently resets the service to first-run, and the failure looks like
"it asked me to sign in again" rather than like data loss. To back the service
up, copy that one file.

Migrations run on every container start, so upgrading is redeploy-and-done.

The app runs behind Coolify's proxy with `proxy_headers` enabled, so it sees
the original scheme and marks the admin session cookie `Secure` when you are on
HTTPS — and leaves it unset for local plain-HTTP development, so both work
without a flag to set. If you ever expose the container's port directly rather
than through a proxy, set `CPLUS_FORWARDED_ALLOW_IPS` to the proxy address
instead of leaving it at the default `*`.

### First-run setup

1. Open `http://localhost:8080`. You land on the sign-in page.
2. **Enter your Seerr URL** (e.g. `http://seerr.local:5055`) and click
   *Sign in with Plex*. A Plex window opens; approve access there.
3. The service asks Seerr who you are and checks Seerr's **ADMIN permission
   bit**. If your account is not the Seerr admin you are refused — the web UI
   has no non-admin use case. The Seerr URL is saved only once it has
   successfully authenticated you, so a typo cannot lock you out.
4. **Configure Prowlarr**: URL and API key, then *Verify Prowlarr connection*.
   Optionally pick a preferred indexer; the default, *All indexers*, is fine.
5. **Create at least one quality profile.** Every Prowlarr-backed action needs
   one. Add filter rules to eliminate candidates and preference rules to rank
   what survives — preference order is what decides ties.
6. **Create actions** — a name, a Prowlarr download client, and a quality
   profile. These become the buttons in the client, e.g. "Stream Now", "Add 4K".
7. **Assign permissions.** Users appear on the Permissions page the first time
   their client signs in, so have each user open the app once, then tick the
   actions they may use — including the built-in *Request* action.

### Environment variables

Only the handful that must exist before the UI does:

| Variable | Default | Purpose |
|---|---|---|
| `CPLUS_PORT` | `8080` | Listen port |
| `CPLUS_HOST` | `0.0.0.0` | Bind address |
| `CPLUS_DB_PATH` | `/data/cplus.db` | SQLite file, on the mounted volume |
| `CPLUS_LOG_LEVEL` | `info` | uvicorn log level |
| `CPLUS_FORWARDED_ALLOW_IPS` | `*` | Which peers' `X-Forwarded-*` headers to trust. Safe as `*` behind a proxy; narrow it if the port is exposed directly |

There is no secret key to set. Admin sessions are opaque random tokens stored in
the database, so there is nothing to sign, rotate or leak — revoking a session
is a row delete, and the sessions live on the same volume as everything else.

State lives entirely in the SQLite file on the `cplus-data` volume. Back that up
and you have backed up the service. `alembic upgrade head` runs on every start,
so upgrading is pull-and-restart.

### Development

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

pytest                      # 390 tests; no network, Prowlarr, Seerr or Plex needed
ruff check .

export CPLUS_DB_PATH=./cplus.db
alembic upgrade head
python -m cplus_service
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
  seerr/client.py       async Seerr API wrapper (auth + allowlisted request ops)
  auth/plex_cache.py    persisted Plex-token -> user mapping (tvOS auth)
  auth/sessions.py      webui browser sessions
  auth/identity.py      Seerr user -> local user upsert
  search/stream.py      IMDB and free-text search, NDJSON phases
  api/app.py            FastAPI factory + lifespan
  api/deps.py           auth/config/client dependencies
  api/routes/           actions, search, grab, request, seerr
  api/routes/admin/     the admin webui: config, profiles, actions,
                        permissions, activity, login (Plex PIN flow)
  plex/client.py        plex.tv PIN flow — webui sign-in only
  web/                  Jinja2 templates + vendored HTMX and CSS
  db/models.py          SQLAlchemy 2.0 schema
  bootstrap.py          seeds the built-in Request action
migrations/             Alembic
docker/entrypoint.sh    migrate, then serve
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
| `config` | singleton row (CHECK-enforced): `seerr_url`, `prowlarr_url`, `prowlarr_api_key`, `preferred_indexer_id`, `plex_client_identifier` |
| `users` | `seerr_user_id` (unique), `plex_username` |
| `quality_profiles` | `name`, `rules` (ordered JSON list) |
| `actions` | `name`, `download_client_id`, `quality_profile_id` |
| `permissions` | user ↔ action, composite PK |
| `grabs` | user, action, release title/guid/indexer/size, `created_at` |
| `activity_log` | user, `event_type` (`search`\|`grab`), `detail` JSON, `created_at` |
| `plex_token_sessions` | SHA-256 token fingerprint → user; what tvOS auth reads |
| `admin_sessions` | opaque browser session tokens for the web UI |

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
   records the token → user mapping in `plex_token_sessions`.
2. `/titles/{imdb_id}/actions`, `/search` and `/grab` authenticate **against
   that mapping only** — no outbound Plex or Seerr call, which is what keeps
   them fast.
3. An unknown token is a `401`; the client's recovery is to call `/actions`,
   which it does on launch anyway.
4. `/request` is the exception: it always validates live, because it needs a
   Seerr session to file the request as the user.

The mapping is **persisted and survives a restart**, so restarting the service
no longer 401s every client until its next launch. Only a SHA-256 fingerprint
is stored, never the token itself, so the table cannot yield a working Plex
credential even if the database file leaks.

There is **no expiry**: an entry is valid until that user's next `/actions`
call overwrites it, or until the user is deleted, which cascades. The tradeoff
is that a Plex token revoked upstream keeps working on `/search` and `/grab`
until one of those happens — removing the user in the admin UI is the immediate
lever. Revoking a single *permission* likewise takes effect at their next
`/actions` call, not at once.

### Webui — Plex OAuth PIN flow + browser session

1. The PIN flow against plex.tv is **proxied server-side** (`POST /admin/plex/pin`,
   then polling `GET /admin/plex/pin/{id}`). The browser only opens the Plex
   popup and polls one URL — the Plex token never reaches page JavaScript.
2. There is exactly one admin sign-in path; the token is validated inside the
   poll handler.
3. The token is validated against Seerr, and Seerr's **ADMIN permission bit**
   (`permissions & 2`) is checked — not `seerr_user_id == 1`, since Seerr grants
   admin through the bitmask and the owner is not guaranteed to be user 1.
4. A non-admin is rejected outright: the webui has no non-admin use case.
5. An admin gets an opaque session token in an httpOnly `cplus_session` cookie,
   backed by the `admin_sessions` table.

`seerr_url` is accepted in the body to make first-run bootstrap possible —
setting it needs an admin session, and getting a session needs it. It is
persisted only after it has been proven to work, so a typo cannot brick config.

**Changing `seerr_url` to a different instance flushes every cached identity**
— both `plex_token_sessions` and `admin_sessions`, wholesale — from both
`POST /admin/config` and the PIN-flow reconnect path. Every row was resolved
against whichever instance was configured when it was written (permissions,
the ADMIN bit, all of it); repointing at a different instance without
dropping those caches would leave every device, and every signed-in browser,
trusting authorization decisions made by an instance that no longer applies.
The admin making the change keeps their own current session — the flush
excludes it — so saving the new URL does not immediately sign them back out.
Every other device and browser gets a clean `401`/redirect on its next call
and re-authenticates, which is already each cache's built-in recovery path.
A save that does not actually change `seerr_url` is a no-op: nothing is
flushed.

---

## Endpoints

### Client (tvOS)

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /actions` | live Seerr | **tvOS only.** Button labels and ids; also the tvOS auth checkpoint |
| `GET /titles/{imdb_id}/actions` | cache | NDJSON stream: releases plus, per permitted action, a recommended release |
| `GET /search?query=` | cache | NDJSON stream, never scored, any category |
| `POST /grab` | cache | `{action_id, release_guid, indexer_id, release_title, size_bytes?}` |
| `POST /manager/grab` | live Seerr | **admin only.** `{download_client_id, release_guid, indexer_id, release_title, size_bytes?}` |
| `GET /manager/download-clients` | live Seerr | **admin only.** Populates the admin app's grab picker |
| `POST /request` | live Seerr | `{tmdb_id, type, seasons?}` |
| `GET /seerr/me` | live Seerr | the caller's Seerr user, verbatim |
| `GET /seerr/requests` | live Seerr | scoped by Seerr: own requests, or all for an admin |
| `POST /seerr/requests/{id}/approve\|decline` | live Seerr | **admin only** |
| `DELETE /seerr/requests/{id}` | live Seerr | own request, or any for an admin |

`/actions` returns just an id and a label, with no title context — it is the
rare, launch-time checkpoint. `GET /titles/{imdb_id}/actions` is the frequent
one: called when tvOS opens a title's detail page, it returns the same id and
label for each permitted action, but scoped to that title and carrying a
`kind` (`"grab"` or `"request"`) and — for a `"grab"` action — its recommended
release, or `null` if nothing survived that action's quality profile. The
client routes a press on `kind`: `"request"` posts to `/request`, `"grab"`
posts to `/grab`. The full release list rides along in the same response, so
"view all releases" needs no second call.

**Actions are a tvOS concept.** They exist to give that app a button label and a
per-action recommendation. The admin app needs neither: it searches by IMDB id
using the free-text-shaped machinery under the hood, then grabs a chosen
release by naming the download client directly — `POST /manager/grab`, with
`download_client_id` instead of `action_id` — restricted to callers who can
manage requests and checked against Seerr live, same as
`GET /manager/download-clients`, which exists to populate that picker since the
web UI's copy is session-gated and the admin app authenticates with a Plex
token.

**Any live Seerr validation refreshes the stored token mapping**, not just
`/actions`. The admin app never calls `/actions`, so without that its first
`/seerr/*` call would leave the mapping empty and `/titles/{imdb_id}/actions`
would 401 for it forever. In practice `GET /seerr/me` at startup is what signs
it in.

`POST /grab` and `POST /manager/grab` both echo back the release fields the
client already received in a search/actions stream. `indexer_id` is what
Prowlarr needs to identify the listing; `release_title` and `size_bytes` are
stored on the `grabs` row so the admin UI's history is readable without
re-querying an indexer for a listing that may no longer exist. `size_bytes` is
optional, because not every indexer reports one — an unknown size is a real
state rather than a client omission. Both bodies reject unknown fields, so
`/grab` cannot be handed a `download_client_id` and `/manager/grab` cannot be
handed an `action_id`.

Because the grab body is self-contained, the server keeps **no state between
a search/actions call and a grab** — a restart in between is harmless.

### Admin

Session-gated, ADMIN-bit-gated, all server-rendered:

| Endpoint | Notes |
|---|---|
| `GET /admin/login` | The only ungated admin route |
| `POST /admin/plex/pin`, `GET /admin/plex/pin/{id}` | Proxied Plex PIN flow |
| `GET/POST /admin/config` | Seerr URL, Prowlarr, preferred indexer |
| `POST /admin/config/verify-prowlarr` | Connect/Verify button |
| `GET /admin/prowlarr/indexers`, `/download-clients` | Proxies, for dropdowns |
| `GET /admin/quality-profiles`, `/new`, `/{id}` | List, create, edit |
| `POST /admin/quality-profiles`, `/rows`, `/{id}/delete` | Save, rule builder, delete |
| `GET/POST /admin/actions`, `POST /admin/actions/{id}`, `/{id}/delete` | Action CRUD |
| `GET /admin/users`, `POST /admin/users/{id}/permissions`, `/{id}/delete` | Permissions |
| `GET /admin/grabs`, `GET /admin/activity-log` | Read-only, filterable by user |

The three proxy/verify endpoints answer **JSON by default** and HTML with
`?format=html`. JSON keeps them usable as an API; the HTML variant is what the
page swaps straight into the DOM.

---

## Streaming search and title actions

Two endpoints, both NDJSON, both built on the same underlying Prowlarr-fetch-
and-score plumbing (`cplus_service.search.stream`) — but they answer different
questions and are not interchangeable:

| | `GET /titles/{imdb_id}/actions` | `GET /search?query=` |
|---|---|---|
| Question answered | "what can I do with this known title?" | "what does Prowlarr have for this string?" |
| Categories | movies only | not scoped — TV, anime, anything |
| Recommendations | one per permitted action, plus the Request action | **never** — always no recommendation |
| Phases | `preferred` then `all`, when a preferred indexer is set | always a single `all` |

`GET /titles/{imdb_id}/actions` is what tvOS calls when a movie's detail page
opens. Response lines carry `releases` (the parsed, tagged, full-disc-filtered
candidates) and `actions` — every action the caller is permitted to use, each
with its `kind` (`"grab"` or `"request"`) and, for a `"grab"` action, the guid
of its recommended release or `null` if nothing survived that action's quality
profile filters. The built-in Request action always appears with
`recommended_release_guid: null` — it has no quality profile and never touches
Prowlarr, so there is nothing to recommend, but the action itself is still
reported so the client always knows which buttons to draw.

`GET /search?query=` is the free-text, action-agnostic browse: a string the
user typed, not a title the client already identified. No quality profile can
meaningfully rank an arbitrary string's results, so none is consulted and
`actions` is always empty — there is nothing to recommend and, with nothing to
race a fast partial answer against, always a single phase. Results are still
parsed, tagged and full-disc-filtered exactly as for a title's actions. Note
the parser is tuned for movie names: a TV release tags resolution, source and
HDR correctly, while `base_title` and `release_group` mean less.

`preferred_only=true` restricts either endpoint to a single call against
`config.preferred_indexer_id`, yielding one `all` phase. It **defaults to
false** — all indexers. With no preferred indexer configured it is a no-op
rather than an error, so a client can send it unconditionally.

**The last line is always `phase: "all"`**, from either endpoint — that is how
a client knows the stream is complete. `preferred` is an optional earlier
partial, and only `GET /titles/{imdb_id}/actions` ever sends one.

For a title's actions with a preferred indexer and `preferred_only=false`, both
Prowlarr calls are issued **concurrently**:

1. scoped to `config.preferred_indexer_id` — **skipped entirely** when that is
   null, since there is nothing distinct to fetch early;
2. across all indexers.

The response is NDJSON, one object per line:

```
{"phase":"preferred","releases":[…],"actions":[{"id":1,"name":"Stream Now","kind":"grab","recommended_release_guid":"guid-a"},{"id":2,"name":"Add 4K","kind":"grab","recommended_release_guid":null}]}
{"phase":"all","releases":[…],"actions":[{"id":1,"name":"Stream Now","kind":"grab","recommended_release_guid":"guid-a"},{"id":2,"name":"Add 4K","kind":"grab","recommended_release_guid":"guid-b"},{"id":3,"name":"Request","kind":"request","recommended_release_guid":null}]}
```

**Client merge rule: apply the last line you received, wholesale.** Union the
`releases` arrays by guid. "View all releases" is a client-side reveal of
`releases` — every candidate is already in the payload, so it needs no second
call.

That is the documented choice for the "re-send all vs. only the unresolved
ones" question: the `all` line always carries every permitted action, so the
client needs no key-level merging and behaves the same whether or not phase 1
ran. The re-sent recommendations never contradict phase 1 — scoring goes
through `preferred_indexer_candidates`, so a non-empty preferred subset keeps
its answer and an empty one falls back to the full set.

`releases` in the `all` line excludes anything already sent in `preferred`,
though clients should tolerate duplicates by guid regardless.

The `all` line is **always** sent — on zero results, and on Prowlarr failure —
so the client can always leave its loading state. Since the response has already
committed to `200` by then, a late failure appears in-band as an `error` field
rather than as a status code. A failure of the *preferred* call alone degrades
silently to a single phase, because the unfiltered search covers that indexer
too.

---

## The `/seerr/*` passthrough

These exist so the Seerr admin API key stops living on client devices — the same
problem this service already solves for Prowlarr, one service over. The client
sends its Plex token, cplus exchanges it for **that user's own** Seerr session,
and makes the call as them.

cplus still holds **no Seerr credential** — only a URL. The session is fetched
fresh per call and discarded, exactly as `/request` always has. That costs one
extra Seerr round trip, which is fine for these: they are user actions and badge
refreshes, not paging.

**It is an allowlist, not a general proxy.** Only these five operations are
reachable. Seerr's `/settings/*` returns the Radarr, Sonarr and Overseerr API
keys to any caller with owner authority, so a blanket proxy would hand them back
out through this service — precisely the leak cplus exists to prevent.

Authorisation is Seerr's, with one addition:

* `GET /seerr/requests` needs no branching here. Seerr scopes it itself — a
  caller without `MANAGE_REQUESTS` or `REQUEST_VIEW` sees only their own
  requests — so one endpoint serves the tvOS app and the admin app correctly.
* **Approve and decline are admin-only**, and cplus refuses a non-admin *before*
  calling Seerr rather than relying on its 403, so the rule is stated in our
  code rather than merely inherited. It matches Seerr's own guard,
  `MANAGE_REQUESTS`, with the owner passing implicitly.
* Delete is left to Seerr's inline rule: your own, or anything if you manage
  requests.

Response bodies are passed through **verbatim**, so a client that previously
talked to Seerr directly only has to change its base URL and swap the API key
for its Plex token.

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

## Admin web UI

Jinja2 + HTMX, server-rendered, no build step and no npm. HTMX is vendored under
`web/static/`, so a container with no outbound access still works.

### The rule builder

The one genuinely interactive piece. Rules are numbered rows with ↑ / ↓ / ×
controls and an add-rule dropdown; filters and preferences are colour-coded
apart, since only preference *order* is meaningful.

It holds **no server-side draft**. Every add, remove and move posts the whole
current form to `POST /admin/quality-profiles/rows`, which decodes it, applies
the operation and re-renders the rows. So two tabs cannot corrupt each other's
draft, an abandoned edit leaves nothing to clean up, and a restart mid-edit
costs nothing. Row indices carry order only and are renumbered on every render.

Ordered rules (`resolution_order`, `source_order`, `hdr_match`, `audio_match`)
use a comma-separated text input rather than a multi-select: order is the whole
point of those rules, and browsers submit multi-select options in document
order, not click order.

Before saving, the decoded rules are validated through stage 1's pydantic
schema. Stored JSON therefore can never hold a shape the engine will not accept,
and an unknown token like `hdr_match: NOT_A_TAG` comes back as a form error
instead of a broken profile.

### Guards worth knowing about

* The built-in **Request** action is listed but read-only, and no other action
  may take its name (`Request`, case-insensitively). The tvOS client routes on
  that name, so renaming or reusing it would silently break every client.
* A quality profile still used by an action cannot be deleted; the page says
  which action is holding it.
* An empty API-key field means "leave the saved key alone", and the saved key is
  never rendered back into the page.
* Removing a user is immediate: it drops their browser sessions *and* evicts
  their cached Plex tokens, which live in memory outside the transaction.
  Revoking a single permission is not immediate — see below.

---

## Cross-stage notes

Three places where a later stage's needs diverged from an earlier stage's
guess. The first two were collapsed; the rest are deliberate.

**Admin verbs drifted from stage 2's stubs.** The stubs named
`PUT /admin/quality-profiles/{id}`, `DELETE /admin/actions/{id}` and
`PUT /admin/users/{id}/permissions`. HTML forms can only issue GET and POST, so
those became `POST .../{id}` and `POST .../{id}/delete`. Stage 2's
`GET /admin/users/{id}/permissions` was dropped — the permissions matrix is
rendered on `GET /admin/users` instead — and stage 3 added routes the stubs did
not anticipate (`/quality-profiles/new`, `/quality-profiles/rows`,
`/users/{id}/delete`, and the login/PIN routes).

**Requests are logged as `grab` events.** `activity_log.event_type` is the
stage-1 enum `search | grab`, and a request is neither a search nor a Prowlarr
grab. It is stored as `grab` with `detail.kind == "request"`, and the activity
page renders it as its own badge. Widening the enum would be cleaner but is a
schema change nobody asked for.

**Permission changes are not immediate.** Revoking an action takes effect at
the user's next `/actions` call, because `/search` and `/grab` authenticate from
the stored token mapping rather than re-checking Seerr. The UI says so rather
than papering over it. Removing the user entirely *is* immediate — the delete
cascades to their token mappings and browser sessions.

### Resolved

*Two admin sign-in paths.* Stage 2's `POST /auth` assumed the browser would run
the PIN flow and hand over a token; stage 3 proxies the flow server-side, so
nothing called it. It has been removed along with `POST /auth/logout`
(superseded by `POST /admin/logout`), leaving one sign-in path and one
admin-bit check.

*Dead `deps.get_admin` / `AdminDep`.* Stage 2 wrote it for stage 3 to wire in,
but a browser needs a redirect rather than 401 JSON. Removed in favour of
`api/routes/admin/deps.require_admin_page`.
