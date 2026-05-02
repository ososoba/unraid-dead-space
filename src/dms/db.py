"""SQLite connection helper.

DB path defaults to `./config/db.sqlite` (matches the Unraid `/config` mount
that will be the runtime default). Opens with WAL mode + foreign keys + a
sensible timeout, returns rows as dict-like `sqlite3.Row` objects.

Use `with conn:` for transactions — Python's default isolation_level wraps
implicit BEGINs around DML and commits on context exit (or rolls back on
exception).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path

DEFAULT_DB_PATH = Path("./config/db.sqlite")


def connect(
    path: str | PathLike[str] = DEFAULT_DB_PATH,
    *,
    foreign_keys: bool = True,
    wal: bool = True,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """Open a SQLite connection with project defaults.

    Caller is responsible for closing (or use as context manager via
    `connection_scope`).
    """
    path = Path(path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    if wal and str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection_scope(
    path: str | PathLike[str] = DEFAULT_DB_PATH,
    **kwargs: object,
) -> Iterator[sqlite3.Connection]:
    """Context manager that opens, yields, and reliably closes a connection."""
    conn = connect(path, **kwargs)  # type: ignore[arg-type]
    try:
        yield conn
    finally:
        conn.close()
