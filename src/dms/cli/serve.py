"""Serve the DMS web app via uvicorn.

Usage:
    python -m dms.cli.serve              # 0.0.0.0:8765 default
    python -m dms.cli.serve --port 9000
    python -m dms.cli.serve --reload     # dev mode, auto-reload on edits
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.cli.serve")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — bind-all is intentional for container
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None, help="SQLite path (overrides ./config/db.sqlite)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on source change")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Must stay 1 (APScheduler + in-process state). >1 is clamped with a warning.",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--access-log",
        dest="access_log",
        action="store_true",
        help="Enable per-request access logs (default off — we don't want IPs in logs)",
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        help=(
            "Comma-separated list of trusted reverse-proxy IPs whose "
            "X-Forwarded-* headers will be honored. Defaults to loopback "
            "(Cloudflared listens on 127.0.0.1). Set to '*' if you fully "
            "trust the network in front of the app."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper())

    if args.workers != 1:
        logger.warning(
            "ignoring --workers=%d; pinned to 1 (APScheduler + in-process "
            "background-task registry require single-worker)",
            args.workers,
        )

    # `factory=True` calls dms.app.create_app() with no args — propagate
    # --db via env so the factory can pick it up.
    if args.db is not None:
        os.environ["DMS_DB_PATH"] = str(args.db)

    uvicorn.run(
        "dms.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
        log_level=args.log_level,
        access_log=args.access_log,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
