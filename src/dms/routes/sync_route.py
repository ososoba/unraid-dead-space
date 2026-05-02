"""Async sync trigger + live status endpoint + /sync page.

Old /config/sync-now blocked the request until the sync finished. This
module supersedes that with:

- POST /sync/run     -> spawns a background task, returns {run_id, started}
- GET  /sync/status  -> JSON snapshot of latest run + per-step status
- GET  /sync         -> minimal htmx page that polls /sync/status

The config page's "Sync now" button is updated to call the new endpoint
in the same commit (templates/config.html).
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dms import auth
from dms.config import load_config
from dms.deps import get_db
from dms.sync.background import is_running, start_background_sync

router = APIRouter()


@router.post("/sync/run", include_in_schema=False)
async def sync_run(
    request: Request,
    user: str = Depends(auth.require_login),
    _: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    config = load_config()
    if not config.arr_instances:
        return JSONResponse({"ok": False, "error": "no Arr instances configured"}, status_code=400)
    db_path = request.app.state.db_path
    started = start_background_sync(db_path, config, kind="manual", requested_by=user)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'sync_now', '/sync/run', ?)",
            (user, json.dumps({"spawned": started})),
        )
    return JSONResponse({"ok": True, "spawned": started, "running": is_running(db_path)})


def _latest_run_with_steps(conn: sqlite3.Connection) -> dict:
    job = conn.execute(
        "SELECT id, kind, status, started_at, finished_at FROM sync_jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if job is None:
        return {"run": None, "steps": []}
    steps = conn.execute(
        "SELECT step_name, status, started_at, finished_at, items_seen, items_changed, "
        "       error_json FROM sync_run_steps WHERE run_id = ? ORDER BY id",
        (job["id"],),
    ).fetchall()
    return {
        "run": {k: job[k] for k in ("id", "kind", "status", "started_at", "finished_at")},
        "steps": [
            {
                k: s[k]
                for k in (
                    "step_name",
                    "status",
                    "started_at",
                    "finished_at",
                    "items_seen",
                    "items_changed",
                    "error_json",
                )
            }
            for s in steps
        ],
    }


@router.get("/sync/status", include_in_schema=False)
async def sync_status(
    request: Request,
    _: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    payload = _latest_run_with_steps(conn)
    payload["task_running"] = is_running(request.app.state.db_path)
    return JSONResponse(payload)


@router.get("/sync/status/fragment", include_in_schema=False, response_class=HTMLResponse)
async def sync_status_fragment(
    request: Request,
    _: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """htmx-friendly partial: returns a small HTML block for live updates."""
    payload = _latest_run_with_steps(conn)
    payload["task_running"] = is_running(request.app.state.db_path)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_sync_status.html",
        {"data": payload},
    )


@router.get("/sync", include_in_schema=False, response_class=HTMLResponse)
async def sync_page(
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    payload = _latest_run_with_steps(conn)
    payload["task_running"] = is_running(request.app.state.db_path)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "sync.html",
        {"user": user, "data": payload},
    )
