"""Config page + actions: settings save, test connection, sync trigger."""

from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from dms import auth, settings_store
from dms.clients.arr import ArrClient
from dms.clients.base import UpstreamError
from dms.clients.requester import RequesterClient
from dms.clients.tautulli import TautulliClient
from dms.config import load_config
from dms.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


def _redact(value: str | None) -> str:
    if not value:
        return ""
    return "•" * 8


@router.get("/config", include_in_schema=False, response_class=HTMLResponse)
async def config_page(
    request: Request,
    _: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    templates = request.app.state.templates
    cfg = load_config()
    instances_status = settings_store.list_instance_status(conn)
    user_mappings = settings_store.list_user_mappings(conn)
    tautulli_users = settings_store.list_tautulli_users(conn)
    last_sync = conn.execute(
        "SELECT id, kind, status, started_at, finished_at FROM sync_jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "user": auth.current_user(request),
            "tunables": settings_store.all_tunables(conn),
            "arr_instances": [
                {
                    "slug": i.slug,
                    "kind": i.kind,
                    "name": i.name,
                    "url": i.url,
                    "api_key_redacted": _redact(i.api_key),
                }
                for i in cfg.arr_instances
            ],
            "tautulli": (
                None
                if cfg.tautulli is None
                else {"url": cfg.tautulli.url, "api_key_redacted": _redact(cfg.tautulli.api_key)}
            ),
            "requester_instances": [
                {
                    "slug": r.slug,
                    "name": r.name,
                    "source": r.source,
                    "url": r.url,
                    "api_key_redacted": _redact(r.api_key),
                }
                for r in cfg.requester_instances
            ],
            "instance_status": instances_status,
            "user_mappings": user_mappings,
            "tautulli_users": tautulli_users,
            "last_sync": last_sync,
        },
    )


@router.post("/config/save", include_in_schema=False)
async def config_save(
    request: Request,
    _: str = Depends(auth.require_login),
    __: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    form = await request.form()
    items = {k: str(v) for k, v in form.items() if k in settings_store.TUNABLE_KEYS}
    try:
        saved = settings_store.save_settings(conn, items)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'save_config', '/config/save', ?)",
            (auth.current_user(request) or "?", json.dumps(saved)),
        )
    return RedirectResponse(url="/config?saved=1", status_code=302)


@router.post("/config/test/{slug}", include_in_schema=False)
async def config_test_connection(
    slug: str,
    request: Request,
    _: str = Depends(auth.require_login),
    __: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    cfg = load_config()
    matches = [i for i in cfg.arr_instances if i.slug == slug]
    if matches:
        ok, detail = await _test_arr(matches[0])
    elif slug == "tautulli" and cfg.tautulli:
        ok, detail = await _test_tautulli(cfg.tautulli)
    else:
        req_match = [r for r in cfg.requester_instances if r.slug == slug]
        if req_match:
            ok, detail = await _test_requester(req_match[0])
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown instance {slug!r}")

    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'test_connection', ?, ?)",
            (auth.current_user(request) or "?", slug, json.dumps({"ok": ok})),
        )
    return JSONResponse({"slug": slug, "ok": ok, "detail": detail})


async def _test_arr(instance) -> tuple[bool, str]:
    try:
        async with ArrClient(instance, timeout=10.0) as client:
            data = await client.system_status()
        return True, f"version={data.get('version', '?')}"
    except UpstreamError as exc:
        return False, str(exc)


async def _test_tautulli(config) -> tuple[bool, str]:
    try:
        async with TautulliClient(config, timeout=10.0) as t:
            info = await t.server_info()
        return True, f"server={info.get('pms_name', '?')}"
    except UpstreamError as exc:
        return False, str(exc)


async def _test_requester(instance) -> tuple[bool, str]:
    try:
        async with RequesterClient(instance, timeout=10.0) as r:
            data = await r.status()
        return True, f"version={data.get('version', '?')}"
    except UpstreamError as exc:
        return False, str(exc)


@router.post("/config/purge-history", include_in_schema=False)
async def config_purge_history(
    request: Request,
    _: str = Depends(auth.require_login),
    __: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    with conn:
        cur = conn.execute("DELETE FROM watch_events")
        deleted = cur.rowcount
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'purge_history', '/config/purge-history', ?)",
            (auth.current_user(request) or "?", json.dumps({"deleted_rows": deleted})),
        )
    return JSONResponse({"ok": True, "deleted_rows": deleted})
