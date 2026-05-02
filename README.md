# Dead Movies & Shows

Private Unraid container that finds unwatched and stale media in your Sonarr/Radarr libraries by joining Tautulli watch history with Overseerr/Seerr request data. Reports reclaim candidates; does not delete.

> Status: **Step 5 done — UI dashboards.** Homepage cards, per-instance deepdive (Never Watched / Stale / Orphans tabs with sort + pagination + scope toggle + "Why is this listed?" drawer), Requesters summary, Ignored list with one-click ignore/restore. Sync UX polish next.

## Quick start (spike)

```bash
# 1. Install
pip install -e .[dev]

# 2. Configure
cp .env.example .env
# Fill in URLs + API keys for your Sonarr/Radarr/Tautulli/Overseerr instances

# 3. Run spike (read-only, no DB)
python -m dms.spike

# 4. Initialize DB (creates ./config/db.sqlite by default)
python -m dms.cli.migrate
python -m dms.cli.migrate --status   # show applied vs pending

# 5. Run a full sync (also auto-applies any pending migrations)
python -m dms.cli.sync --pretty

# 6. Set up auth + start the web app
python -m dms.cli.hash_password   # paste output into APP_PASSWORD_HASH in .env
python -c "import secrets; print(secrets.token_urlsafe(32))"  # set SESSION_SECRET
python -m dms.cli.serve
# Then http://localhost:8765 — login → /config
```

The spike prints JSON to stdout: per-instance counts, identity-map stats, top candidates by reason. Use `--limit N` to cap rows, `--reason <name>` to filter.

## Documentation

- [`PLAN.md`](./PLAN.md) — full implementation plan, schema, decisions log.
- [`dead-space-attack.md`](./dead-space-attack.md) — Codex critique notes.
- [`CLAUDE.md`](./CLAUDE.md) — Claude project context.

## Roadmap

See `PLAN.md` § Build Order. Web UI, sync pipeline, and Unraid template come after the spike validates the data layer.
