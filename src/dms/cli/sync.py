"""Run a full sync against configured upstreams.

Usage:
    python -m dms.cli.sync                # full sync, default DB path
    python -m dms.cli.sync --db PATH      # custom SQLite path
    python -m dms.cli.sync --kind manual  # mark run kind in sync_jobs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from dms.config import load_config
from dms.db import DEFAULT_DB_PATH, connect
from dms.migrations import apply_pending
from dms.sync.runner import run_sync

log = logging.getLogger("dms.cli.sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.cli.sync", description="Run a DMS sync")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path")
    parser.add_argument("--kind", choices=["full", "incremental", "manual"], default="manual")
    parser.add_argument("--requested-by", default="cli", help="Marker stored in sync_jobs")
    parser.add_argument("--no-migrate", action="store_true", help="Skip auto-migrate")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print summary JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config()
    if not config.arr_instances:
        log.error("No Sonarr/Radarr instances configured. Fill in .env first.")
        return 2

    conn = connect(args.db)
    try:
        if not args.no_migrate:
            apply_pending(conn)
        summary = asyncio.run(
            run_sync(conn, config, kind=args.kind, requested_by=args.requested_by)
        )
    finally:
        conn.close()

    indent = 2 if args.pretty else None
    json.dump(asdict(summary), sys.stdout, indent=indent, default=str)
    sys.stdout.write("\n")
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
