# Dead Movies & Shows — Project Context

## Overview
Private Unraid container that identifies unwatched/stale media across Sonarr/Radarr instances using Tautulli watch history and Overseerr/Seerr request data. Reports candidates for deletion to reclaim disk space. Read-only against external systems in v1.

## Stack
- Python 3.11+, FastAPI + Uvicorn, Jinja2 + htmx, SQLite (WAL).
- httpx (async) for upstream APIs, pydantic for models, pydantic-settings for env loading.
- Tailwind CLI build (no CDN), APScheduler with single-worker + DB lock.
- Container: `python:3.11-slim`, multi-arch, entrypoint handles PUID/PGID.

## Build / Test
- Install dev deps: `pip install -e .[dev]`
- Lint: `ruff check src tests`
- Format: `ruff format src tests`
- Test: `pytest`
- Run CLI spike: `python -m dms.spike` (reads `.env`, prints JSON to stdout)
- Apply DB migrations: `python -m dms.cli.migrate` (default `./config/db.sqlite`)
  - `--list` shows all known migrations, `--status` shows applied vs pending
- Run a full sync: `python -m dms.cli.sync --pretty` (auto-applies migrations)
- Generate a password hash: `python -m dms.cli.hash_password` (paste into APP_PASSWORD_HASH)
- Run the web app: `python -m dms.cli.serve` (default `0.0.0.0:8765`, `--reload` for dev)

## Architecture
- `src/dms/clients/` — async API clients (Sonarr/Radarr, Tautulli, Overseerr/Seerr).
- `src/dms/identity.py` — GUID parsing + external-ID resolution (rating_key → tmdb/tvdb/imdb).
- `src/dms/candidates.py` — bucket / candidate engine.
- `src/dms/spike.py` — read-only CLI that joins all sources and emits JSON.
- `src/dms/db.py` — SQLite connection helper (WAL, FK, dict-row factory).
- `src/dms/migrations/` — versioned SQL migrations + runner; `0001_initial.sql` is the full 19-table schema.
- `src/dms/cli/migrate.py` — `python -m dms.cli.migrate` runs pending migrations.
- `src/dms/cli/sync.py` — `python -m dms.cli.sync` runs a full sync.
- `src/dms/cli/serve.py` — `python -m dms.cli.serve` runs the FastAPI web app.
- `src/dms/cli/hash_password.py` — bcrypt hash generator for APP_PASSWORD_HASH.
- `src/dms/sync/` — sync pipeline modules:
  - `locks.py` (heartbeat + stale recovery), `runs.py` (job + step bookkeeping),
    `upsert.py` (UPSERT + tombstones), `arr_sync.py`, `plex_sync.py`,
    `tautulli_sync.py`, `requester_sync.py`, `attribution.py`, `watch_state.py`,
    `candidates_db.py`, `runner.py` (orchestrator).
- `src/dms/app.py` — FastAPI app factory (lifespan auto-runs migrations).
- `src/dms/auth.py` — session cookie + CSRF + login_required dep.
- `src/dms/deps.py` — per-request DB dependency.
- `src/dms/settings_store.py` — config-table-backed settings + env precedence.
- `src/dms/routes/` — HTTP routes: `login`, `healthz`, `config_route`,
  `home` (`/`), `instance` (`/instance/{slug}`), `requesters`, `ignored`,
  `sync_route` (`/sync`, `/sync/run`, `/sync/status`).
- `src/dms/scheduler.py` — APScheduler cron job at `SYNC_CRON`.
- `src/dms/sync/background.py` — fire-and-forget runner with in-process dedup.
- `src/dms/views/` — read-side query helpers (`candidates`, `summary`).
- `src/dms/formatters.py` — Jinja filters (humansize, humandate, percent).
- `src/dms/templates/`, `src/dms/static/` — Jinja2 + plain CSS (Tailwind in Step 7).

## Authoritative docs
- `PLAN.md` — full implementation plan, schema, decisions log. Update when decisions change.
- `dead-space-attack.md` — Codex critique notes that informed PLAN.md v2.

## Conventions
- Files ≤300 lines, functions ≤50 lines.
- All function signatures typed.
- Pydantic models for all upstream API responses (validation at the boundary).
- Try/except at integration boundaries (HTTP calls); let logic errors surface.
- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
- Stage specific files; never `git add .` or `git commit -a`.
- Secrets only in `.env` (gitignored). Never log API keys.

## Build order — feature-complete
1. CLI spike (read-only, no DB). ✓
2. Schema + migrations. ✓
3. Sync pipeline. ✓
4. FastAPI shell + auth. ✓
5. UI dashboards. ✓
6. Sync UX (htmx live progress, scheduler, partial-failure banner). ✓
7. Containerization (Dockerfile, entrypoint, healthcheck, compose). ✓
8. Unraid template (CA-compatible XML). ✓
9. **CI (GitHub Actions: ruff + pytest + buildx + ghcr publish).** ✓

## Container

`Dockerfile` is multi-stage (`python:3.11-slim` builder → slim runtime), runs
`tini` as PID 1, drops privileges via `gosu` per PUID/PGID. The bundled
`entrypoint.sh` chowns `/config`. `docker-compose.yml` is for local dev.

Tailwind was originally planned but skipped: ~80 lines of plain CSS does the
job and avoids adding a Node toolchain to the image. Marked v2 if needed.
4. FastAPI shell + auth.
5. UI.
6. Sync UX.
7. Containerization.
8. Unraid template.
9. CI.
