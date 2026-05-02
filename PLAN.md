# Dead Movies & Shows — Implementation Plan

> Status: locked, ready to build. Step 1 = CLI spike.
> Companion doc: `dead-space-attack.md` (Codex review that informed v2 of this plan).

---

## 1. Goal

Identify movies and shows in a private media library that are unwatched or stale, so they can be considered for deletion to reclaim disk space. Private single-user app, runs as a Docker container on Unraid, sits behind a Cloudflared tunnel and is also reachable on plain HTTP over LAN.

## 2. Integrations

- **Sonarr** × 2 instances (split by quality: 1080p, 4K)
- **Radarr** × 2 instances (split by quality: 1080p, 4K)
- **Tautulli** — watch history + Plex library inventory
- **Request service** — Overseerr (current) and Seerr (future migration). Jellyseerr stub trivially derived since it's Overseerr-compatible.

Files in different Arr instances are treated as **independent** for space accounting (a movie in both Radarr 1080p and Radarr 4K = two independent rows).

## 3. Stack

- **Language:** Python 3.11+
- **Web:** FastAPI + Uvicorn (single worker, enforced) + Jinja2 + htmx
- **CSS:** Tailwind, compiled once at Docker build via Tailwind CLI (no CDN — Tailwind Play CDN is dev-only).
- **DB:** SQLite (WAL mode), stored at `/config/db.sqlite`
- **HTTP:** `httpx` (async) with timeouts + retry policy + concurrency cap
- **Scheduler:** APScheduler (cron). Single worker + DB-level `sync_locks` row to prevent duplicate syncs.
- **Auth:** form login → signed session cookie via `itsdangerous`. Password stored as bcrypt hash in env.
- **Container:** `python:3.11-slim`, multi-arch (amd64 + arm64), entrypoint handles PUID/PGID + chown `/config` + drops privileges, healthcheck on `/healthz`.

## 4. Configuration (env vars)

```env
# Auth
APP_USERNAME=
APP_PASSWORD_HASH=        # bcrypt hash; helper script generates this
SESSION_SECRET=
COOKIE_SECURE=false       # default false: works on both LAN HTTP and tunnel HTTPS
SESSION_DAYS=90

# Unraid / runtime
PUID=99
PGID=100
TZ=America/Toronto

# Sync
SYNC_CRON=0 4 * * *
SYNC_MAX_CONCURRENCY=4
HTTP_TIMEOUT_SECONDS=30
BACKFILL_PAGE_SIZE=500
SYNC_LOCK_TTL_MINUTES=120

# Watch logic
WATCH_SCOPE=anyone        # anyone | requester (UI toggle overrides)
WATCH_THRESHOLD_MOVIES_PCT=80
WATCH_THRESHOLD_EPISODES_PCT=80
SERIES_SPECIALS_MODE=ignore  # ignore | include
NEVER_WATCHED_DAYS=90
STALE_DAYS=180
HISTORY_RETENTION_YEARS=10

# Arr instances (numbered, supports N of each)
SONARR_1_NAME=Sonarr 1080p
SONARR_1_URL=
SONARR_1_API_KEY=
SONARR_2_NAME=Sonarr 4K
SONARR_2_URL=
SONARR_2_API_KEY=

RADARR_1_NAME=Radarr 1080p
RADARR_1_URL=
RADARR_1_API_KEY=
RADARR_2_NAME=Radarr 4K
RADARR_2_URL=
RADARR_2_API_KEY=

# Tautulli
TAUTULLI_URL=
TAUTULLI_API_KEY=

# Request services (numbered; supports multiple instances of any flavor)
# Each instance can be a different source — handy during Overseerr → Seerr migration,
# or when you split request services by quality (e.g. Overseerr 1080p + Overseerr 4K).
REQUESTER_1_SOURCE=overseerr   # overseerr | seerr | jellyseerr | none
REQUESTER_1_NAME=Overseerr 1080p
REQUESTER_1_URL=
REQUESTER_1_API_KEY=

REQUESTER_2_SOURCE=overseerr
REQUESTER_2_NAME=Overseerr 4K
REQUESTER_2_URL=
REQUESTER_2_API_KEY=
```

In-app config page can override most values (DB-stored values take precedence over env after first save). Connection settings have a per-instance **Test connection** button.

## 5. Data Model

Sized to be correct, not minimal. Separates Arr inventory from Plex inventory from watch events from request attribution.

```text
instances
  id, kind (sonarr|radarr), name, url, api_key, enabled,
  last_seen_ok_at, last_error, created_at, updated_at

arr_items
  id, instance_id, kind (movie|series), arr_id, title, year,
  tmdb_id, tvdb_id, imdb_id,
  monitored, added_at, path, root_folder, quality_profile,
  ignored_local (bool),
  last_seen_sync_run_id, deleted_at

arr_episodes
  id, instance_id, series_arr_id, episode_id,
  season_number, episode_number, absolute_episode_number,
  title, air_date, monitored, has_file, episode_file_id, is_special

arr_files
  id, instance_id, arr_item_id,
  kind (movie|episode),
  arr_file_id,                       -- Sonarr episodeFile id or Radarr movieFile id
  arr_episode_id (nullable, episode kind only),
  season_number (nullable),
  episode_number (nullable),
  path, size_bytes, date_added, quality,
  last_seen_sync_run_id, deleted_at

plex_items
  id, rating_key, parent_rating_key, grandparent_rating_key,
  kind (movie|show|season|episode),
  title, year, section_id, section_name,
  tmdb_id, tvdb_id, imdb_id, guid, guids_json,
  -- episode coordinates (episode kind only):
  season_number, episode_number, absolute_episode_number,
  parent_title, grandparent_title, originally_available_at,
  last_seen_sync_run_id, deleted_at

plex_media_files
  id, plex_item_id, rating_key,
  file_path, size_bytes,
  container, video_resolution, video_codec,
  last_seen_sync_run_id, deleted_at

watch_events
  id, source_row_id (Tautulli row_id, unique),
  rating_key, parent_rating_key, grandparent_rating_key,
  kind, user_id, user_name,
  -- denormalized episode coordinates for query speed:
  season_number, episode_number,
  started_at, stopped_at,
  percent_complete, watched_status, play_duration_sec

tags
  id, instance_id, arr_id, raw_tag,
  parsed_requester_id, parsed_requester_name, is_unparseable

requests
  id, source (overseerr|seerr|jellyseerr|tag|manual),
  source_request_id, media_kind, tmdb_id, tvdb_id,
  requester_id, requester_name, requested_at, status

request_attribution
  arr_item_id, requester_id, requester_name, source
  -- denormalized; recomputed each sync; resolution order:
  --   1. requests table (overseerr/seerr) earliest request wins
  --   2. parsed tag (single)
  --   3. multiple parsed tags → "multi-requester"
  --   4. none → attributed to "me"

user_identity_map
  id,
  requester_source (overseerr|seerr|jellyseerr|tag|manual|self),
  requester_id, requester_name,
  tautulli_user_id, tautulli_user_name, plex_username,
  match_method (api|name|manual|self),
  confidence (high|medium|low),
  created_at, updated_at
  -- resolves requester → Tautulli/Plex user for requester-scope watch state.
  -- "self" maps the owner's own account.
  -- Unresolved requesters mark their candidates as low-confidence in the UI.

watch_state
  arr_item_id,
  has_any_play (bool), has_requester_play (bool),
  total_episodes,
  episodes_watched_count_anyone,
  episodes_watched_count_requester,
  episode_coverage_pct_anyone,
  episode_coverage_pct_requester,
  last_played_at_anyone, last_played_at_requester,
  is_finished_anyone, is_finished_requester,
  requester_mapping_confidence (nullable; from user_identity_map)

candidates
  id,
  arr_item_id (nullable),            -- null for plex-only orphans
  plex_item_id (nullable),           -- null for arr-only orphans
  plex_media_file_id (nullable),
  reason,
  scope (anyone|requester),
  size_bytes, age_days, last_played_at,
  confidence (high|medium|low),      -- low if requester scope + unresolved mapping
  computed_at_sync_run_id
  -- CHECK (arr_item_id IS NOT NULL OR plex_item_id IS NOT NULL)
  -- reason ∈ {
  --   never_watched_anyone, never_watched_requester,
  --   stale_finished_anyone, stale_finished_requester,
  --   stale_partial_anyone, stale_partial_requester,
  --   orphan_arr_no_plex, orphan_plex_no_arr
  -- }

ignore_rules
  id, scope (arr_item|tmdb|tvdb), arr_item_id, tmdb_id, tvdb_id,
  reason, created_at, created_by

sync_jobs
  id, kind (full|incremental|manual), status,
  requested_by, started_at, finished_at,
  cursor_json, error_json

sync_run_steps
  id, run_id, step_name, status,
  started_at, finished_at,
  items_seen, items_changed, error_json

sync_locks
  name (PK), acquired_at, heartbeat_at, owner
  -- heartbeat refreshed during sync; lock stealable if heartbeat older
  -- than SYNC_LOCK_TTL_MINUTES; stale recovery audit-logged.

audit_log
  id, ts, actor, action, target, details_json
  -- actions: ignore, unignore, purge_history, save_config,
  --          test_connection, sync_now, login, login_fail,
  --          stale_lock_recovered, user_mapping_saved

config
  key (PK), value, updated_at
```

Indexes on all `tmdb_id`, `tvdb_id`, `rating_key`, `user_id`, `started_at`, `last_seen_sync_run_id`, `(season_number, episode_number)`.

### Identity resolution

Tautulli `get_history` rows do **not** carry tmdb/tvdb directly. Identity map is built explicitly:

1. Tautulli `get_libraries_table` + `get_library_media_info` → populate `plex_items` with rating_key + GUIDs (`tmdb://`, `tvdb://`, `imdb://`) + episode coordinates for episode kind. Populate `plex_media_files` with file path, size, container, resolution.
2. Arr items already carry tmdb/tvdb/imdb. `arr_files` carries paths and sizes.
3. **Watch-state matching** uses external IDs: `watch_events.rating_key` → `plex_items` → external IDs → `arr_items`.
4. **Orphan / file-presence matching** uses file/path/resolution against `plex_media_files`, not external IDs alone. This prevents false positives where Radarr 1080p and Radarr 4K share a tmdb_id but only one quality file is actually in Plex.

Treat watch history as events, not as the canonical identity map.

### User identity resolution (requester ↔ Tautulli/Plex)

Requester scope depends on knowing which Tautulli `user_id` corresponds to a given Overseerr/Seerr requester. There is no guaranteed one-to-one mapping by name, so:

1. On request-service sync, populate `user_identity_map` rows using API data where available (Overseerr/Seerr expose Plex/Jellyfin/Emby user IDs on the user object — match by Plex username when present).
2. Fall back to fuzzy name match (`requester_name == tautulli_user_name`, case-insensitive) at `medium` confidence.
3. The owner's own account gets a `self` row.
4. The config UI surfaces unresolved requesters with a Tautulli-user dropdown for manual mapping (`match_method = manual`, `confidence = high`).
5. Requester-scope candidates whose mapping is unresolved are tagged `confidence = low` in the `candidates` table and visually flagged in the UI.

## 6. Sync Pipeline

Triggered by APScheduler on `SYNC_CRON` or manually. Async. Posts per-step progress to `sync_run_steps` for live UI updates.

Per run:

1. **Acquire `sync_locks` row** (named `global_sync`).
   - Reject "Sync now" if locked AND heartbeat fresh (`heartbeat_at` within `SYNC_LOCK_TTL_MINUTES`).
   - If heartbeat is stale, steal the lock and write a `stale_lock_recovered` audit entry.
   - Refresh `heartbeat_at` every 30s during the run.
   - Release in a `finally` block.
2. **Arr pull** — for each enabled instance, fetch items + episodes + files (`arr_files` covers both movie and episode kinds). Tag rows with `last_seen_sync_run_id`. Soft-delete with `deleted_at` for rows missing this run (only if the run was successful for that instance).
3. **Tag parsing** — pull tag definitions, parse format `"<id> - <name>"`, mark unparseable.
4. **Plex inventory pull** — Tautulli `get_libraries_table` + per-section `get_library_media_info`. Upsert `plex_items` (with episode coordinates for episode kind) and `plex_media_files` (paths/sizes/resolutions).
5. **Tautulli history pull** — `get_history` paginated. First run: full backfill capped at 10 years, **resumable** via `sync_jobs.cursor_json`. Subsequent: incremental from `MAX(row_id)` (Tautulli's stable PK), with a 24h `started_at` overlap window as safety. Denormalize season/episode onto `watch_events`.
6. **Request service pull** — Overseerr/Seerr `/api/v1/request` paginated. Earliest request per media wins for attribution.
7. **User identity map refresh** — populate/update `user_identity_map` from request-service user data (Plex/Jellyfin/Emby user IDs) and Tautulli user list. Fuzzy name fallback at medium confidence; manual mappings preserved.
8. **Identity resolution** — link `watch_events` to `arr_items` via `plex_items` GUIDs (watch-state matching). Link `arr_files` to `plex_media_files` via path/size (file-presence matching).
9. **Request attribution recompute** — populate `request_attribution`.
10. **Watch state recompute** — per `arr_item`, compute anyone vs requester counters separately (using `user_identity_map` to filter requester plays). Carry forward `requester_mapping_confidence`.
11. **Candidate engine** — rebuild `candidates` table fully:
    - Compute new candidates with the current `sync_run_id`.
    - UI queries only the latest successful `computed_at_sync_run_id`.
    - Old candidate rows pruned, keeping the last 3 sync runs for debugging.
    - Tag `confidence = low` on requester-scope rows where mapping is unresolved.
12. **Release lock**, write `sync_jobs.finished_at`.

### Failure handling

- Per-instance partial failure: mark `sync_run_steps.status = failed` with `error_json`, **keep last-known-good data** for that instance, banner the UI. Do not soft-delete arr_items belonging to a failed instance.
- HTTP timeouts + retry policy (exponential backoff, max 3 retries) per upstream call.
- Concurrency capped via `SYNC_MAX_CONCURRENCY`.

## 7. Bucket / Candidate Logic

Buckets are **filtered views over the `candidates` table**, not rigid enums.

### Reasons

- `never_watched_anyone` — `has_any_play = false` AND `now - added_at > NEVER_WATCHED_DAYS`.
- `never_watched_requester` — `has_requester_play = false` AND `now - added_at > NEVER_WATCHED_DAYS` AND requester is known.
- `stale_finished_anyone` — `is_finished_anyone = true` AND `now - last_played_at_anyone > STALE_DAYS`.
- `stale_finished_requester` — same, but for requester.
- `stale_partial_anyone` — has plays but not finished AND `now - last_played_at_anyone > STALE_DAYS`.
- `stale_partial_requester` — same, requester scope.
- `orphan_arr_no_plex` — `arr_files` row has no matching `plex_media_files` row (file-level match by path or size+resolution). Stored on `candidates` with `arr_item_id` populated, `plex_item_id` null.
- `orphan_plex_no_arr` — `plex_media_files` row has no matching `arr_files` row. Stored with `arr_item_id` null, `plex_item_id` + `plex_media_file_id` populated.

### Definitions

- **Movie finished** = watched once past threshold.
- **Series finished (anyone)** = all available episodes (excluding specials by default, see `SERIES_SPECIALS_MODE`) watched by at least one user.
- **Series finished (requester)** = all available episodes watched by the resolved requester (via `user_identity_map`). Requires a resolved mapping; otherwise low-confidence.
- **Watch threshold** = percentage, separate for movies vs episodes.
- **Watch percentage for shows in UI** = scope-aware: `episodes_watched_count_anyone / total_episodes` when scope = anyone, `episodes_watched_count_requester / total_episodes` when scope = requester. Computed from `arr_episodes` ↔ `plex_items` ↔ `watch_events` joins.

### Active state vs new-season-after-finished

If a series was finished + stale, then a new season drops, `last_episode_added_at` is surfaced in the UI and the candidate appears as `stale_partial_anyone` (or `_requester`) — the new unwatched episodes flip the finished state. UI shows "new content downloaded since last watch."

### Ignored

Rows with `ignored_local = true` are excluded from `candidates` (computed at sync time). Visible on `/ignored` with a Restore button.

## 8. UI

### Pages

- **`/login`** — form, sets signed session cookie.
- **`/`** (Homepage)
  - Top cards: total reclaimable space + count, broken down by `never_*` vs `stale_*`, with anyone/requester scope toggle.
  - Age-of-download bucket table (0–30 / 30–90 / 90–365 / 1y+) for never-watched: count + size.
  - Last sync timestamp + "Sync now" button.
  - Partial-failure banner if any instance failed in the last sync.
- **`/instance/<id>`** (Per-instance deepdive)
  - Tabs: **Never Watched | Stale | Orphans | Ignored**.
  - Filter chips on Stale: `All | Finished | Partial`.
  - Global toggle: **Watch scope: Anyone / Requester**.
  - Configurable sort (size desc default; also last_played, added_at, episode_coverage).
  - Configurable page size, paginated.
  - Columns: title, year, requester, added_at, last_played_at, last_episode_added_at (series), watch %, size, actions.
  - Per-row "Why is this listed?" expand drawer:
    ```
    Reason: requester never watched
    Requester: Alex via Seerr request #1234
    Requester ↔ Plex user: "alex_p" (high confidence, manual mapping)
    Added: 2025-07-01
    Last watched by anyone: 2024-12-03 by user "moyin"
    Last watched by requester: never
    Episode coverage (anyone): 8/10
    Episode coverage (requester): 2/10
    Plex match: file present (rating_key 51234, /movies/4k/foo.mkv, 38.2 GB)
    Arr instance: Radarr 4K
    Confidence: high
    ```
  - Per-row Ignore button (one-click, undo from `/ignored`).
  - Low-confidence rows visually flagged with a warning badge.
- **`/requesters`**
  - Table: requester, total items, total size, broken down by reason.
  - Click through to filtered list per requester.
- **`/config`**
  - Per-instance connection settings + Test connection (API keys redacted as `••••••••` after save).
  - Threshold knobs (NEVER_WATCHED_DAYS, STALE_DAYS, watch %, specials mode, watch scope default).
  - Sync schedule (display only; cron is env-driven, change requires restart).
  - History tools: total stored history size, **Purge all history** button (confirmation modal).
  - **Requester ↔ Plex user mapping**: list of requesters from request-service, current mapping (auto/manual/unresolved) + confidence badge, dropdown of Tautulli users for manual override. Saving writes `user_identity_map` and triggers a watch-state recompute on next sync.
  - Last sync details + per-instance status.
- **`/sync`** — htmx live progress while sync runs.
- **`/ignored`** — list of ignored items with Restore.
- **`/healthz`** — auth-exempt healthcheck.

### Actions

- **Ignore** (one-click, undo): app-local exclusion only. Audit-logged. Does NOT touch Sonarr/Radarr.
- **Test connection**: hits `system/status` (Arr), `get_server_info` (Tautulli), `/api/v1/status` (Overseerr/Seerr).
- **Sync now**: triggers immediate run if `sync_locks` is free.
- **Purge history**: deletes all `watch_events`, audit-logged.
- **(Future) Delete from Arr**: schema + audit log already accommodate it. Will gate behind explicit feature flag.

## 9. Security

- CSRF tokens on all state-changing POSTs (ignore, unignore, purge, save config, sync-now, test connection, login).
- Session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` per `COOKIE_SECURE` env, signed with `SESSION_SECRET`, 90-day expiry.
- Password stored as bcrypt hash (`APP_PASSWORD_HASH`), helper CLI to generate.
- Trust `X-Forwarded-Proto` / `X-Forwarded-For` (Cloudflared) via Starlette `ProxyHeadersMiddleware`.
- **No IP logging.** Disable Uvicorn access logs or replace with sanitized logger.
- API keys: redacted in logs, redacted in UI after save, never in audit `details_json`, never in sync error JSON.
- `/config` directory permissions: 0700, owned by runtime user (handled in entrypoint).
- DB file: 0600.

## 10. Deployment

### Files

- `Dockerfile` — `python:3.11-slim`, multi-stage with Tailwind CLI build, non-root user, entrypoint handles PUID/PGID + chown `/config` + drop privs, `HEALTHCHECK` on `/healthz`.
- `entrypoint.sh` — `gosu`-based user mapping.
- `docker-compose.yml` — for local dev.
- `unraid-template.xml` — private Unraid template, CA-compatible structure but not published:
  - WebUI URL uses **container port** (e.g. `http://[IP]:[PORT:8765]/`).
  - Single `/config` mount → `/mnt/user/appdata/dead-movies-shows`.
  - All env vars with descriptions + sensible defaults.
  - Default `PUID=99`, `PGID=100`, `TZ=America/Toronto`.
  - Icon and metadata included. Support thread URL and license fields only required if publishing publicly later.
- `.env.example` — every var documented.
- `.gitignore`, `.dockerignore` — per global standards.
- `README.md` — setup, env reference, FAQ.
- `CLAUDE.md` — project-specific Claude context.

### Logs

stdout only. Sync diagnostics also surfaced in UI so users don't need container logs.

### CI

`.github/workflows/ci.yml`:
- ruff check + format
- pytest
- docker buildx (amd64 + arm64), no push
- (optional) push to ghcr.io on tag

## 11. Repo Layout

```
unraid-space-saver/
├── src/
│   ├── dms/
│   │   ├── __init__.py
│   │   ├── app.py                  # FastAPI app factory
│   │   ├── auth.py                 # session, CSRF, login
│   │   ├── config.py               # env + DB-backed settings
│   │   ├── db.py                   # SQLite, migrations
│   │   ├── scheduler.py            # APScheduler setup
│   │   ├── spike.py                # CLI spike entry point (step 1)
│   │   ├── clients/
│   │   │   ├── arr.py              # Sonarr/Radarr (shared, both v3)
│   │   │   ├── tautulli.py
│   │   │   └── requester.py        # Overseerr/Seerr/Jellyseerr adapter
│   │   ├── sync/
│   │   │   ├── runner.py           # orchestrates sync run
│   │   │   ├── arr_sync.py
│   │   │   ├── plex_sync.py        # Tautulli library inventory
│   │   │   ├── tautulli_sync.py    # history
│   │   │   ├── requester_sync.py
│   │   │   ├── identity.py         # rating_key → guid → external IDs
│   │   │   ├── attribution.py      # request_attribution
│   │   │   ├── watch_state.py
│   │   │   └── candidates.py       # candidate engine
│   │   ├── routes/
│   │   │   ├── home.py
│   │   │   ├── instance.py
│   │   │   ├── requesters.py
│   │   │   ├── config_page.py
│   │   │   ├── sync_page.py
│   │   │   ├── ignored.py
│   │   │   └── login.py
│   │   ├── templates/              # Jinja2
│   │   ├── static/
│   │   │   └── css/                # compiled Tailwind output
│   │   └── cli/
│   │       └── hash_password.py    # generate APP_PASSWORD_HASH
├── tests/
├── docs/
├── scripts/
├── .github/
│   └── workflows/
│       └── ci.yml
├── unraid-template.xml
├── Dockerfile
├── entrypoint.sh
├── docker-compose.yml
├── tailwind.config.js
├── .env.example
├── .gitignore
├── .dockerignore
├── pyproject.toml                  # ruff, pytest, deps
├── README.md
├── CLAUDE.md
├── PLAN.md                         # this file
└── dead-space-attack.md            # Codex review notes
```

## 12. Build Order

1. **CLI spike** (`python -m dms.spike`)
   - Clients: Sonarr, Radarr, Tautulli (history + library), Overseerr, Seerr.
   - Identity map: rating_key → guids → tmdb/tvdb/imdb.
   - Candidate engine producing JSON output.
   - Read-only API calls, no DB writes.
   - Validate against real library.
2. **Schema + migrations** — full data model.
3. **Sync pipeline** — runner, per-step status, tombstones, resumable Tautulli backfill, partial-failure handling.
4. **FastAPI shell** — auth (form + cookie + CSRF + bcrypt), config page with redacted keys, test-connection.
5. **UI** — homepage, instance deepdive with candidate-reason filters + scope toggle, requesters page, "Why is this listed?" drawer, ignored page.
6. **Sync UX** — htmx progress polling, partial-failure banners, sync-now with lock check.
7. **Containerization** — Dockerfile + entrypoint + Tailwind CLI build, multi-arch, healthcheck.
8. **Unraid template** — XML, validated.
9. **CI** — ruff + pytest + buildx.

## 13. Notable Decisions Log

| # | Decision | Why |
|---|---|---|
| 1 | Watch threshold = percentage, per library type | More meaningful than absolute minutes; movies and episodes need different thresholds |
| 2 | Treat Arr instances independently for accounting | User splits 1080p/4K, wants both reported |
| 3 | Tautulli history mirrored locally, capped 10y, purgeable | Survives Tautulli pruning; user can reset |
| 4 | Requester resolution: requests table → tag → multi-requester → "me" | Overseerr/Seerr most authoritative; tags are fallback |
| 5 | Watch scope toggle (anyone vs requester) | "Did the requester actually watch what they asked for?" is a killer feature |
| 6 | Candidate engine with reasons, not rigid buckets | UI explains *why* an item is listed; extensible |
| 7 | Episode/file model split | Series state can't live in a flat row |
| 8 | Plex library inventory ≠ watch history | Orphan detection requires actual inventory |
| 9 | Tailwind CLI build, not CDN | Production correctness |
| 10 | Single Uvicorn worker + DB sync lock | Prevents duplicate scheduled jobs |
| 11 | CSRF on every POST | Standard hardening, especially behind tunnel |
| 12 | Entrypoint handles PUID/PGID | Bare python image doesn't get this for free |
| 13 | Ignore is app-local only | Safer than touching Sonarr/Radarr monitor flag |
| 14 | Build CLI spike first | Validate identity map before any UI work |
| 15 | Adapter pattern for Overseerr/Seerr/Jellyseerr | API-compatible siblings; one client, `flavor` flag |
| 16 | `COOKIE_SECURE=false` default | Avoid LAN HTTP vs tunnel HTTPS dual-cookie footgun |
| 17 | Report-only v1, deletion gated for future | Audit log + schema accommodate it |
| 18 | Generic `arr_files` table (movie + episode) | Movies need a file table too for size accounting; one table simpler than two |
| 19 | `plex_media_files` separate from `plex_items` | Independent instance accounting requires file-level orphan matching, not external-ID matching |
| 20 | `candidates.arr_item_id` and `plex_item_id` both nullable | `orphan_plex_no_arr` has no arr_item by definition |
| 21 | `user_identity_map` table for requester ↔ Tautulli/Plex user | Names don't map cleanly across systems; required for requester-scope correctness |
| 22 | Per-scope episode coverage stored separately | UI scope toggle is meaningless with a single coverage figure |
| 23 | Episode coordinates on `plex_items` + denormalized on `watch_events` | Series watch state needs to map at episode granularity, not show level |
| 24 | `sync_locks` heartbeat + TTL stale recovery | Container crash mid-sync would otherwise wedge the lock forever |
| 25 | Candidates fully rebuilt per successful sync, last 3 retained | UI always reads latest; debugging history available |
| 26 | N numbered requester instances (Overseerr/Seerr/Jellyseerr) | Mirrors Sonarr/Radarr split-by-quality pattern; supports Overseerr→Seerr migration with both running |
| 27 | `.env` access blocked via PreToolUse hook + permission denies | Prevents Claude from reading or echoing secrets during automation; `.env.example` allowed |

## 14. Open Items / v2+

- **Delete from Arr** action (gated behind feature flag).
- **Per-show season-level breakdown** in deepdive.
- **Duplicate quality copy** badge (informational only, not a candidate reason — user explicitly wants instances treated independently).
- **Plex direct integration** if Tautulli library inventory turns out to be lossy.
- **Notification webhooks** (Discord/Pushover) on candidate threshold breaches.

---

*Generated 2026-05-02 from the Q&A session, revised after two Codex reviews (see `dead-space-attack.md` and inline decisions log entries 18–25). Canonical source of truth for build decisions; update as decisions evolve.*
