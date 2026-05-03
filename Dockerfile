# syntax=docker/dockerfile:1.7
# Multi-stage build for Dead Movies & Shows.
# - builder stage: install deps + build wheel into a dedicated venv
# - runtime stage: slim Debian + Python, copy venv, drop privileges via entrypoint
#
# Note on Tailwind: PLAN.md called for a Tailwind CLI build step. The current
# UI uses ~80 lines of plain CSS that work fine; adding a Node toolchain to
# the image for that small surface adds ~100MB and a build dependency for no
# user-visible benefit. Marking as v2 in CLAUDE.md. The static CSS is shipped
# as-is in src/dms/static/.

# ---------- builder ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System build deps (kept out of the runtime image).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Create an isolated venv we copy into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies first (pyproject only) so layer cache survives source edits.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip \
 && pip install .

# ---------- runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DMS_DB_PATH=/config/db.sqlite \
    PUID=99 \
    PGID=100 \
    TZ=UTC

# Runtime deps:
#   gosu     — drop from root → app user inside the entrypoint
#   tini     — PID 1 init that reaps zombies + forwards signals (uvicorn-friendly)
#   curl     — used by HEALTHCHECK
#   tzdata   — TZ env variable resolution
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu tini curl tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 100 dms || true \
 && useradd  --system --uid 99 --gid 100 --home-dir /app --no-create-home --shell /usr/sbin/nologin dms || true

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src/ /app/src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config"]
EXPOSE 8765

# /healthz is auth-exempt. --max-time 3 keeps unhealthy checks from hanging.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --silent --fail --max-time 3 http://127.0.0.1:8765/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
# Note: --access-log is opt-in (default off) per the Codex review pass —
# we don't want IPs in container logs. Don't add --no-access-log here:
# that flag was removed when the default flipped, and passing it crashes
# argparse in a restart loop.
CMD ["python", "-m", "dms.cli.serve", "--host", "0.0.0.0", "--port", "8765"]
