"""Apply pending DB migrations.

Usage:
    python -m dms.cli.migrate            # apply pending against ./config/db.sqlite
    python -m dms.cli.migrate --db PATH  # custom path
    python -m dms.cli.migrate --list     # list known migrations and exit
    python -m dms.cli.migrate --status   # show applied vs pending
"""

from __future__ import annotations

import argparse
import logging
import sys

from dms.db import DEFAULT_DB_PATH, connect
from dms.migrations import applied_versions, apply_pending, list_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.cli.migrate", description="Apply DMS migrations")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite DB")
    parser.add_argument("--list", action="store_true", help="List known migrations and exit")
    parser.add_argument("--status", action="store_true", help="Show applied / pending and exit")
    parser.add_argument("--quiet", action="store_true", help="Suppress info logs")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.list:
        for m in list_migrations():
            print(f"{m.version:04d}  {m.name}")
        return 0

    if args.status:
        conn = connect(args.db)
        try:
            applied = applied_versions(conn)
            for m in list_migrations():
                marker = "applied " if m.version in applied else "pending "
                print(f"{marker} {m.version:04d}  {m.name}")
        finally:
            conn.close()
        return 0

    conn = connect(args.db)
    try:
        applied = apply_pending(conn)
    finally:
        conn.close()

    if applied:
        print(f"Applied {len(applied)} migration(s):")
        for m in applied:
            print(f"  {m.version:04d}  {m.name}")
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
