# Dead Movies & Shows

Private Unraid container that finds unwatched and stale media in your Sonarr/Radarr libraries by joining Tautulli watch history with Overseerr/Seerr request data. Reports reclaim candidates; does not delete.

> Status: **Step 2 done — schema + migrations.** Step 1 (read-only CLI spike) and Step 2 (SQLite schema, 19 tables, idempotent migration runner) are in. Sync pipeline next.

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
```

The spike prints JSON to stdout: per-instance counts, identity-map stats, top candidates by reason. Use `--limit N` to cap rows, `--reason <name>` to filter.

## Documentation

- [`PLAN.md`](./PLAN.md) — full implementation plan, schema, decisions log.
- [`dead-space-attack.md`](./dead-space-attack.md) — Codex critique notes.
- [`CLAUDE.md`](./CLAUDE.md) — Claude project context.

## Roadmap

See `PLAN.md` § Build Order. Web UI, sync pipeline, and Unraid template come after the spike validates the data layer.
