"""SQLite connection helper.

DB path defaults to `./config/db.sqlite` (matches the Unraid `/config` mount
that will be the runtime default). Opens with WAL mode + foreign keys + a
sensible timeout, returns rows as dict-like `sqlite3.Row` objects.

Use `with conn:` for transactions — Python's default isolation_level wraps
implicit BEGINs around DML and commits on context exit (or rolls back on
exception).

PLAN.md §9 requires the DB file to be 0600. SQLite creates the main file
+ `-wal` + `-shm` siblings under whatever umask the process inherits, so
we explicitly chmod them after opening.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("./config/db.sqlite")
DB_FILE_MODE = 0o600
_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _chmod_db_files(path: Path) -> None:
    """chmod the SQLite triplet to 0600. Best-effort — Windows ignores."""
    if str(path) == ":memory:":
        return
    for suffix in _DB_SUFFIXES:
        sibling = path.with_name(path.name + suffix) if suffix else path
        if not sibling.exists():
            continue
        try:
            current = stat.S_IMODE(sibling.stat().st_mode)
            if current != DB_FILE_MODE:
                os.chmod(sibling, DB_FILE_MODE)
        except OSError as exc:
            logger.debug("chmod %s failed (likely Windows / non-POSIX FS): %s", sibling, exc)


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
        # FastAPI may dispatch sync handlers on threadpools; we open one
        # connection per request and never share it. Disable the same-thread
        # guard so async-then-threadpool handoff is safe.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    if wal and str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    _chmod_db_files(path)
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
