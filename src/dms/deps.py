"""FastAPI dependencies that don't fit in `auth` or route modules."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from dms.db import connect


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request DB connection. Opens one, closes in response phase."""
    db_path = request.app.state.db_path
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
