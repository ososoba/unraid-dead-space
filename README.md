# Dead Movies & Shows

[![CI](https://github.com/ososoba/unraid-dead-space/actions/workflows/ci.yml/badge.svg)](https://github.com/ososoba/unraid-dead-space/actions/workflows/ci.yml)

Private Unraid container that finds unwatched and stale media in your Sonarr/Radarr libraries by joining Tautulli watch history with Overseerr/Seerr request data. Reports reclaim candidates; does not delete.

> Status: **Step 9 done — feature-complete.** All 9 steps in PLAN.md are shipped: CLI spike, schema + migrations, sync pipeline (with locks/tombstones/resumable backfill), FastAPI shell + auth, dashboards, htmx live sync UX, scheduler, container, Unraid template, GitHub Actions CI publishing multi-arch images to ghcr.io.

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

## Install on Unraid

1. **First push to `main` (or any `vX.Y.Z` tag) builds the image.** CI
   publishes `ghcr.io/ososoba/unraid-dead-space:latest` (and `:main` /
   `:vX.Y.Z`) for both `linux/amd64` and `linux/arm64`. Until that first
   green run, no image exists yet — wait for the badge above to go green.
2. **Add the template:** Unraid → Docker → "Add Container" → Template URL:
   ```
   https://raw.githubusercontent.com/ososoba/unraid-dead-space/main/unraid-template.xml
   ```
3. **Fill in 5 required fields:**
   - WebUI port (default `8765` is fine)
   - Appdata path (default `/mnt/user/appdata/dead-movies-shows` is fine)
   - App username (default `admin` is fine)
   - **App password hash** — generate via:
     ```
     docker run --rm ghcr.io/ososoba/unraid-dead-space:latest python -m dms.cli.hash_password --plain YOUR_PASSWORD
     ```
   - **Session secret** — generate via `openssl rand -base64 32`.
4. Open "Show advanced" to fill in your Sonarr/Radarr/Tautulli/Overseerr URLs
   and API keys. Optional: tweak watch thresholds, sync cron, timezone.
5. Apply, wait for the container's healthcheck to go green, browse to the
   WebUI link in Unraid's Docker tab, sign in.

The spike prints JSON to stdout: per-instance counts, identity-map stats, top candidates by reason. Use `--limit N` to cap rows, `--reason <name>` to filter.

## Documentation

- [`PLAN.md`](./PLAN.md) — full implementation plan, schema, decisions log.
- [`dead-space-attack.md`](./dead-space-attack.md) — Codex critique notes.
- [`CLAUDE.md`](./CLAUDE.md) — Claude project context.

## Roadmap

See `PLAN.md` § Build Order. Web UI, sync pipeline, and Unraid template come after the spike validates the data layer.
