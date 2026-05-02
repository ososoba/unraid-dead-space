"""Serve the DMS web app via uvicorn.

Usage:
    python -m dms.cli.serve              # 0.0.0.0:8765 default
    python -m dms.cli.serve --port 9000
    python -m dms.cli.serve --reload     # dev mode, auto-reload on edits
"""

from __future__ import annotations

import argparse
import logging

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.cli.serve")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — bind-all is intentional for container
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on source change")
    parser.add_argument("--workers", type=int, default=1, help="Must stay 1 for APScheduler")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--no-access-log",
        action="store_true",
        help="Disable per-request access logs (we don't log IPs)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper())

    uvicorn.run(
        "dms.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level=args.log_level,
        access_log=not args.no_access_log,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
