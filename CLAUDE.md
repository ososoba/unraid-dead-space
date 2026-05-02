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

## Architecture
- `src/dms/clients/` — async API clients (Sonarr/Radarr, Tautulli, Overseerr/Seerr).
- `src/dms/identity.py` — GUID parsing + external-ID resolution (rating_key → tmdb/tvdb/imdb).
- `src/dms/candidates.py` — bucket / candidate engine.
- `src/dms/spike.py` — read-only CLI that joins all sources and emits JSON.
- `src/dms/db.py` — SQLite connection helper (WAL, FK, dict-row factory).
- `src/dms/migrations/` — versioned SQL migrations + runner; `0001_initial.sql` is the full 19-table schema.
- `src/dms/cli/migrate.py` — `python -m dms.cli.migrate` runs pending migrations.
- (Future) `src/dms/sync/`, `src/dms/routes/`, etc.

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

## Build order (current step in bold)
1. CLI spike (read-only, no DB). ✓
2. **Schema + migrations.** ✓
3. Sync pipeline.
4. FastAPI shell + auth.
5. UI.
6. Sync UX.
7. Containerization.
8. Unraid template.
9. CI.
