Your plan is good enough to build from, but I would **not** start with the skeleton yet. The dangerous parts are not FastAPI, htmx, or SQLite; they are the **data joins, “dead” definitions, API assumptions, and Unraid operational details**. The current plan assumes the hard parts are implementation details, but a few of them can change the product’s output from “useful” to “silently wrong.” 

## Biggest attacks

### 1. The Tautulli join assumption is too optimistic

The plan says Tautulli rows give `tmdb_id` / `tvdb_id`, so joins work without relying on `rating_key` drift. I would not trust that as written. Tautulli’s `get_history` example exposes fields like `guid`, `rating_key`, `parent_rating_key`, `grandparent_rating_key`, `percent_complete`, and `watched_status`, but not clean first-class `tmdb_id` / `tvdb_id` columns in the history row. Tautulli’s `get_metadata` can expose richer `guids` / `grandparent_guids` arrays such as `imdb://`, `tmdb://`, and `tvdb://`, but that is a separate metadata lookup/cache problem, not a free history join. ([GitHub][1])

**Fix:** add a `plex_items` / `plex_guid_map` table and explicitly resolve Plex rating keys to external IDs. Treat Tautulli history as watch events, not as your canonical identity map.

---

### 2. Orphan detection is currently flawed

This line is dangerous: “Arr with no Plex history + no Plex match → `arr_no_plex`.” No Plex history does **not** imply no Plex item; it may simply be never watched. For orphan detection you need a Plex/Tautulli **library inventory**, not watch history. Tautulli has library/media endpoints that expose library sections, media rows, file sizes, rating keys, play counts, and library metadata, which is much closer to what you need for Plex-side inventory. ([GitHub][1])

**Fix:** split Plex ingestion into two separate feeds: `plex_library_items` and `tautulli_watch_events`. Orphans should be based on Arr inventory versus Plex library inventory. Watch history should only influence buckets.

---

### 3. Series need a real episode/file model

The plan says `media_items` is “one row per file-instance,” but the fields are a hybrid of movie, series, and aggregate series state: `total_episodes`, `last_episode_added_at`, `episode_coverage_pct`, etc. That will break down for multi-episode files, specials, deleted episodes, renamed files, season packs, anime numbering, alternate episode orders, and partially available shows.

**Fix:** use at least four concepts:

```text
arr_items          movie or series identity per Arr instance
arr_episode_files  Sonarr file rows, size, path, date added
arr_episodes       season/episode metadata, monitored, has_file, air_date, absolute number
watch_events       Plex/Tautulli play rows, resolved to movie/show/season/episode where possible
```

Then derive series-level state from episode/file state. Do not make series rows carry all episode truth directly.

---

### 4. “Watched by any user” may make the app lie

Your current definition says finished series = all available episodes watched by **any** user, and movie finished = watched once past threshold. That may be fine for “can I reclaim disk space,” but it is wrong for “did the requester use what they asked for?” A movie requested by User A but watched by User B will be marked alive or finished even if User A never touched it.

**Fix:** make the core report configurable:

```text
Dead for everyone: no one watched it
Dead for requester: requester never watched it
Stale for everyone: no one has watched recently
Stale for requester: requester has not watched recently
```

For a requester-driven app, “requester never watched” is probably the killer feature.

---

### 5. Overseerr-only support is stale in 2026

The plan names Overseerr as the request source. As of February 2026, Overseerr is being superseded by Seerr, which merges Overseerr and Jellyseerr into a unified project; the Overseerr repository says it will no longer receive major updates, and LinuxServer’s Overseerr image is marked deprecated with a recommendation to migrate to Seerr. ([Seerr][2])

**Fix:** rename the integration layer to `request_services`, not `overseerr`. Support adapters:

```text
overseerr legacy
jellyseerr legacy
seerr current
tag fallback
manual override
```

The DB table should not be called only `overseerr_requests`; use `requests` with `source = overseerr|jellyseerr|seerr|tag|manual`.

---

### 6. Tailwind CDN is the wrong production choice

The plan says Tailwind via CDN because it avoids a build step. Official Tailwind docs say the Play CDN is for development and is not intended for production. Tailwind’s production guidance is to generate only the styles actually used and ship a static CSS file. ([Tailwind CSS][3])

**Fix:** either use plain CSS, Pico.css, DaisyUI compiled once, or a tiny Tailwind CLI build during Docker build. A build step is less annoying than shipping a production Unraid app that depends on a browser-side CSS generator/CDN.

---

### 7. APScheduler inside FastAPI needs a single-runner guarantee

FastAPI/Uvicorn can run multiple worker processes; each process has its own memory and startup path. The FastAPI docs show multiple workers each running application startup, which means an in-app scheduler can duplicate jobs unless you force one worker or implement a lock. ([FastAPI][4])

**Fix:** for v1, run exactly one Uvicorn worker and enforce a DB-level sync lock anyway:

```sql
sync_locks(name primary key, acquired_at, owner)
```

Also reject or queue “Sync now” if a sync is already running. Do not rely on “it’s a private app” here; duplicate syncs will corrupt state or cause confusing partial-failure reports.

---

### 8. Unraid template details are underspecified

The plan includes an Unraid Community Apps XML template, but the template is not a formality. Unraid docs say Community Applications submissions need documentation, support resources, open-source licensing unless proprietary components require otherwise, and compatibility/maintenance expectations. The WebUI port in the XML also needs to reference the **container port**, not the host port; this is a common template mistake called out by a Community Applications maintainer. ([Unraid Docs][5])

**Fix:** promote these from “deployment artifacts” to acceptance criteria:

```text
support thread URL exists
license chosen
icon/logo present
WebUI uses container port, e.g. http://[IP]:[PORT:8765]
/config is the only persistent mount
no default host share paths that create surprise shares
template validates in CA
```

---

## Security attacks

The auth plan is undersized. A signed cookie is fine, but the state-changing routes need CSRF protection. Ignore, unignore, purge history, save config, test connection, and sync-now are all actions that should require a CSRF token. SameSite=Lax helps, but it is not a full substitute if this app is exposed through Cloudflared.

The env names `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` are misleading because the app is not using HTTP Basic Auth; it is using form login plus a signed session. Rename them to `APP_USERNAME` and `APP_PASSWORD` or, better, `APP_PASSWORD_HASH`.

API keys will be stored in SQLite if the config page can edit them. That is probably acceptable for a private Unraid app, but the plan should explicitly say: redact keys in logs, redact in UI after save, never include them in audit details, chmod the DB/config directory, and do not dump config values into sync failure JSON.

Also, “no IP logging” conflicts with default web-server access logs. Uvicorn access logs include client info by default. If no IP logging is a requirement, disable access logs or replace them with sanitized logs.

---

## Data-model attacks

I would change these tables before writing code:

```text
instances
  id, kind, name, url, api_key_encrypted_or_plain_redacted, enabled,
  last_seen_ok_at, last_error, created_at, updated_at

arr_items
  id, instance_id, kind, arr_id, title, year,
  tmdb_id, tvdb_id, imdb_id,
  monitored, added_at, path, root_folder, quality_profile,
  last_seen_sync_run_id, deleted_at

arr_episode_files
  id, instance_id, series_arr_id, episode_file_id,
  path, size_bytes, date_added, quality, last_seen_sync_run_id

arr_episodes
  id, instance_id, series_arr_id, episode_id,
  season_number, episode_number, absolute_episode_number,
  title, air_date, monitored, has_file, episode_file_id, is_special

plex_items
  id, rating_key, parent_rating_key, grandparent_rating_key,
  kind, title, year, section_id, section_name,
  tmdb_id, tvdb_id, imdb_id, guid, guids_json,
  file_size_bytes, last_seen_sync_run_id

watch_events
  source_row_id, rating_key, parent_rating_key, grandparent_rating_key,
  kind, user_id, user_name, started_at, stopped_at,
  percent_complete, watched_status, play_duration_sec,
  unique(source_row_id)

requests
  source, source_request_id, media_kind, tmdb_id, tvdb_id,
  requester_id, requester_name, requested_at, status

request_attribution
  arr_item_id, requester_id, requester_name, source, confidence

ignore_rules
  id, scope, arr_item_id, instance_id, tmdb_id, tvdb_id,
  reason, created_at, created_by
```

The key change: do not make `media_items` do everything. Separate Arr inventory, Plex inventory, watch events, and request attribution.

---

## Sync-pipeline attacks

The first sync should not be a monolithic “full backfill capped at 10 years.” Large Tautulli installs can have a lot of rows. Make backfill resumable from day one.

Use this model:

```text
sync_jobs
  id, kind, status, requested_by, started_at, finished_at,
  cursor_json, error_json

sync_run_steps
  run_id, step_name, status, started_at, finished_at,
  items_seen, items_changed, error_json
```

For Tautulli, store uniqueness on Tautulli `row_id` where available. The plan’s “MAX(started_at) minus 24h overlap” is good as a safety window, but not enough as the only cursor. Clock weirdness, edits, grouped rows, and historical imports can make timestamp-only syncs miss or duplicate things.

For Arr sync, use `last_seen_sync_run_id` and tombstones. Do not immediately delete rows missing from a failed or partial sync. If Sonarr #2 fails, you need to know whether a missing item is truly gone or just not refreshed.

Add request timeouts, retry policy, and concurrency limits. A single slow Arr instance should not freeze the whole app.

---

## Product-definition attacks

The buckets are too few. “Never,” “stale,” “partial,” and “active” are useful, but users will ask different questions:

```text
Never watched by anyone
Never watched by requester
Partially watched and abandoned
Finished and stale
Series with new unwatched episodes
Plex missing from Arr
Arr missing from Plex
Duplicate across instances
```

I would make “dead” a saved filter, not a single enum. Something like:

```text
candidate_reason:
  never_watched_anyone
  never_watched_requester
  stale_finished
  stale_partial
  orphan_arr_no_plex
  orphan_plex_no_arr
  duplicate_quality_copy
```

That lets the UI explain *why* something is being shown. A mysterious bucket value will make users distrust the app.

---

## Unraid/container attacks

`PUID=99` and `PGID=100` do nothing by magic in a plain `python:3.11-slim` image. LinuxServer images advertise easy PUID/PGID mappings because their image stack handles that pattern; a bare Python image needs an entrypoint that creates/modifies the runtime user, fixes `/config` ownership, and drops privileges correctly. ([GitHub][6])

For a simple v1, I would either:

```text
Option A: use a known Unraid-friendly base pattern with s6 / user mapping
Option B: keep python:3.11-slim but run as a fixed non-root UID and document /config ownership
Option C: accept PUID/PGID but implement entrypoint logic properly
```

Also add:

```text
/config/db.sqlite
/config/backups/
/config/app.log?  # or stdout only, but not both
```

If logs are stdout-only, the app should still expose sync diagnostics in the UI, because Unraid users should not need to inspect container logs to know which API failed.

---

## UI attacks

The homepage summary is useful, but it should not lead with “total dead space” until the user understands whether that includes 1080p + 4K duplicates, requester-only misses, ignored items, and orphans. Otherwise the headline number will look more precise than it is.

Add a “Why is this listed?” drawer per row:

```text
Reason: requester never watched
Requester: Alex via Seerr request #1234
Added: 2025-07-01
Last watched by anyone: never
Plex match: yes
Arr instance: Radarr 4K
Size: 38.2 GB
Confidence: high
```

That explanation drawer matters more than the summary cards.

---

## Build-order attack

Your current build order starts with skeleton/auth/config. I would invert it. The riskiest part is not the web shell; it is whether the app can correctly identify media across Arr, Plex/Tautulli, and request systems.

Better build order:

```text
1. API/data spike with CLI only:
   - pull one Radarr
   - pull one Sonarr
   - pull Tautulli library inventory
   - pull Tautulli history
   - pull Seerr/Overseerr requests
   - print joined candidates as JSON

2. Build identity map:
   - rating_key -> guid -> tmdb/tvdb/imdb
   - Arr IDs -> external IDs
   - request IDs -> external IDs
   - confidence scoring

3. Build bucket engine with tests:
   - movies
   - series
   - requester watched vs anyone watched
   - specials ignored/included
   - stale partial vs stale finished

4. Only then build FastAPI/auth/UI.

5. Then Docker/Unraid template.

6. Then sync progress/polish.
```

This prevents you from spending time on a nice UI that reports wrong candidates.

---

## What I would change in the plan immediately

Rename the app’s core from “Dead Movies & Shows” logic to a **candidate engine**. “Dead” is the UI label; internally these are reclaim candidates with reasons and confidence.

Change `OVERSEERR_*` to request-service config:

```text
REQUESTER_SOURCE=seerr|overseerr|jellyseerr|none
REQUESTER_URL=
REQUESTER_API_KEY=
```

Change `BASIC_AUTH_*`:

```text
APP_USERNAME=
APP_PASSWORD_HASH=
SESSION_SECRET=
COOKIE_SECURE=auto
```

Add:

```text
SYNC_MAX_CONCURRENCY=4
HTTP_TIMEOUT_SECONDS=30
BACKFILL_PAGE_SIZE=500
ENABLE_REQUESTER_MODE=true
WATCH_SCOPE=anyone|requester
SERIES_SPECIALS_MODE=ignore|include
```

Add DB tables for `plex_items`, `arr_episodes`, `arr_episode_files`, `requests`, `sync_run_steps`, and `ignore_rules`.

Add CSRF tokens for all POST actions.

Replace Tailwind CDN with generated static CSS.

Make Seerr the preferred request source and Overseerr/Jellyseerr compatibility adapters.

---

## Verdict

The plan is **directionally strong**, but it is currently too confident about media identity and watch-state derivation. The P0 fixes are:

```text
1. Add Plex library inventory, not just Tautulli history.
2. Add explicit rating_key/guid/external-ID resolution.
3. Split series into episode/file tables.
4. Support Seerr/Jellyseerr/Overseerr through an adapter.
5. Define watched-by-requester versus watched-by-anyone.
6. Add sync locking and resumable backfill.
7. Fix auth naming, CSRF, and secret redaction.
8. Replace Tailwind CDN.
9. Make the Unraid template a first-class deliverable, not an afterthought.
```

Also change the line "Unraid Community Apps XML template" to "Private Unraid XML template, compatible with Docker user templates and CA Private Apps."

After those changes, this becomes a very buildable and useful Unraid app. As written, the biggest risk is not that it fails loudly; it is that it quietly produces plausible but wrong “dead media” lists.

[1]: https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference "Tautulli API Reference · Tautulli/Tautulli Wiki · GitHub"
[2]: https://docs.seerr.dev/blog/seerr-release "Seerr Release: Unifying Overseerr and Jellyseerr | Seerr"
[3]: https://tailwindcss.com/docs/installation/play-cdn "Play CDN - Tailwind CSS"
[4]: https://fastapi.tiangolo.com/deployment/concepts/ "Deployments Concepts - FastAPI"
[5]: https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/community-applications/ "Community Applications | Unraid Docs"
[6]: https://github.com/linuxserver/docker-overseerr "GitHub - linuxserver/docker-overseerr · GitHub"
