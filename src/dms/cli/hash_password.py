"""Generate a bcrypt hash for APP_PASSWORD_HASH.

Usage:
    python -m dms.cli.hash_password               # prompts (no echo)
    python -m dms.cli.hash_password --plain XYZ   # accept plain on argv

Print the hash to stdout. Paste into .env as:
    APP_PASSWORD_HASH=<the printed string>
"""

from __future__ import annotations

import argparse
import getpass
import sys

import bcrypt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.cli.hash_password")
    parser.add_argument(
        "--plain", default=None, help="Password as a CLI arg (avoid in shells with history)"
    )
    parser.add_argument("--rounds", type=int, default=12, help="bcrypt cost factor (default 12)")
    args = parser.parse_args(argv)

    if args.plain is not None:
        password = args.plain
    else:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm:  ")
        if password != confirm:
            print("error: passwords do not match", file=sys.stderr)
            return 2

    if not password:
        print("error: password may not be empty", file=sys.stderr)
        return 2

    digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=args.rounds))
    print(digest.decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
