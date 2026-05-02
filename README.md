# Dead Movies & Shows

Private Unraid container that finds unwatched and stale media in your Sonarr/Radarr libraries by joining Tautulli watch history with Overseerr/Seerr request data. Reports reclaim candidates; does not delete.

> Status: **Step 7 done — containerization.** Multi-stage Dockerfile, entrypoint with PUID/PGID + `gosu` privilege drop, `tini` PID 1, `/healthz` Docker healthcheck, `docker-compose.yml` for local dev. Unraid template next.

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

## Run as a container

Image is built from the bundled `Dockerfile`. The `dms` user inside the image
defaults to UID 99 / GID 100 (Unraid's `nobody:users`); set `PUID` / `PGID`
to override.

```bash
docker compose up --build           # local dev with .env + ./config volume
# or
docker build -t dms .
docker run -d --name dms \
  -p 8765:8765 \
  -v /mnt/user/appdata/dms:/config \
  --env-file .env \
  -e PUID=99 -e PGID=100 -e TZ=America/Toronto \
  --restart unless-stopped \
  dms
```

The container:
- runs `tini` as PID 1, which exec's the entrypoint;
- entrypoint reconciles UID/GID, chowns `/config`, drops to `dms` via `gosu`;
- exec's `python -m dms.cli.serve` on `0.0.0.0:8765`;
- exposes `/healthz` for Docker's `HEALTHCHECK` (every 30s).

`/config` is the only writable mount — SQLite DB lives there.

The spike prints JSON to stdout: per-instance counts, identity-map stats, top candidates by reason. Use `--limit N` to cap rows, `--reason <name>` to filter.

## Documentation

- [`PLAN.md`](./PLAN.md) — full implementation plan, schema, decisions log.
- [`dead-space-attack.md`](./dead-space-attack.md) — Codex critique notes.
- [`CLAUDE.md`](./CLAUDE.md) — Claude project context.

## Roadmap

See `PLAN.md` § Build Order. Web UI, sync pipeline, and Unraid template come after the spike validates the data layer.
