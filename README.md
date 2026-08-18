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

This repository currently contains **stage 1 of 3**.

| | Component | Status |
|---|---|---|
| 1 | Data model + migrations | ✅ done |
| 1 | Release parser | ✅ done |
| 1 | Prowlarr client wrapper | ✅ done |
| 1 | Quality profile rule engine | ✅ done |
| 2 | Plex/Seerr auth, HTTP endpoints, Request action | not started |
| 3 | Streaming, admin web UI | not started |

Nothing in stage 1 needs a running HTTP server. Everything is verifiable through
`pytest` and `scripts/demo.py`.

---

## Getting started

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

pytest                      # 176 tests, no network or Prowlarr needed
python scripts/demo.py      # parse a canned release set and recommend per profile
ruff check .
```

Against a real Prowlarr:

```bash
export PROWLARR_URL=http://prowlarr.local:9696
export PROWLARR_API_KEY=...
export PREFERRED_INDEXER_ID=3        # optional; unset means "All indexers"
python scripts/demo.py tt1160419
```

Create the database:

```bash
export CPLUS_DB_PATH=./cplus.db      # optional; defaults to ./cplus.db
alembic upgrade head
```

---

## Layout

```
src/cplus_service/
  release/models.py    ParsedTitle / ParsedRelease — the stable client contract
  release/parser.py    title -> structured metadata; drops full discs
  quality/models.py    quality profile rule schema (pydantic, discriminated union)
  quality/engine.py    recommend(candidates, profile) -> ParsedRelease | None
  prowlarr/client.py   async Prowlarr API wrapper
  prowlarr/models.py   Indexer / DownloadClient / SystemStatus / GrabResult
  db/models.py         SQLAlchemy 2.0 schema
  db/session.py        async engine, session factory, config singleton accessor
migrations/            Alembic
scripts/demo.py        offline + live REPL-style driver
tests/                 unit tests for all of the above
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
| `is_prerelease` | CAM / HDCAM / TS / telesync / telecine / screener / R5 / workprint / DCP |
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

## For stage 2

The contract above is stable — stage 2 and the tvOS client both depend on it.
What stage 2 builds on top:

* Plex auth validated against the admin's Seerr instance.
* Search endpoint: fetch via `ProwlarrClient.search_movie`, apply
  `preferred_indexer_candidates`, then call `recommend` once per action the
  caller is permitted to use. Return the flat tagged list plus the per-action
  recommendation — no sections, no categories.
* Grab endpoint: check `permissions`, call `ProwlarrClient.grab` with the
  action's `download_client_id`, write `grabs` + `activity_log`.
* The built-in Request action (the one exception to movies-only).
