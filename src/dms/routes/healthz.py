"""Auth-exempt health check.

Returns 200 + {"ok": true, ...} when the app can open the DB. Used by
the Docker HEALTHCHECK.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from dms.deps import get_db
from dms.migrations import applied_versions

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    versions = sorted(applied_versions(conn))
    return {
        "ok": True,
        "schema_versions": versions,
    }
