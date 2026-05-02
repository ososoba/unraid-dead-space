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


@router.post("/config/user-mapping", include_in_schema=False)
async def config_save_user_mapping(
    request: Request,
    _: str = Depends(auth.require_login),
    __: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Manually map a requester to a Tautulli user (PLAN.md §8 config page).

    Form: requester_source, requester_id, tautulli_user_id (or '' to clear).
    The new row is marked match_method='manual' / confidence='high' so the
    next sync's auto-refresh leaves it alone.
    """
    form = await request.form()
    source = (form.get("requester_source") or "").strip()
    try:
        requester_id = int(form.get("requester_id") or "")
    except ValueError:
        raise HTTPException(400, "requester_id must be an int") from None

    raw_tautulli = (form.get("tautulli_user_id") or "").strip()
    tautulli_user_id: int | None = None
    tautulli_user_name: str | None = None
    if raw_tautulli:
        try:
            tautulli_user_id = int(raw_tautulli)
        except ValueError:
            raise HTTPException(400, "tautulli_user_id must be an int") from None
        # Look up the friendly name from watch_events for the audit log + UI.
        row = conn.execute(
            "SELECT user_name FROM watch_events WHERE user_id = ? LIMIT 1",
            (tautulli_user_id,),
        ).fetchone()
        if row is not None:
            tautulli_user_name = row["user_name"]

    existing = conn.execute(
        "SELECT requester_name FROM user_identity_map "
        "WHERE requester_source = ? AND requester_id = ?",
        (source, requester_id),
    ).fetchone()
    requester_name = existing["requester_name"] if existing else None

    with conn:
        conn.execute(
            """
            INSERT INTO user_identity_map
              (requester_source, requester_id, requester_name,
               tautulli_user_id, tautulli_user_name, plex_username,
               match_method, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, 'manual', 'high', datetime('now'))
            ON CONFLICT(requester_source, requester_id) DO UPDATE SET
              tautulli_user_id   = excluded.tautulli_user_id,
              tautulli_user_name = excluded.tautulli_user_name,
              match_method       = 'manual',
              confidence         = 'high',
              updated_at         = excluded.updated_at
            """,
            (source, requester_id, requester_name, tautulli_user_id, tautulli_user_name),
        )
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'user_mapping_saved', ?, ?)",
            (
                auth.current_user(request) or "?",
                f"{source}:{requester_id}",
                json.dumps(
                    {
                        "tautulli_user_id": tautulli_user_id,
                        "tautulli_user_name": tautulli_user_name,
                    }
                ),
            ),
        )
    return JSONResponse(
        {
            "ok": True,
            "requester_source": source,
            "requester_id": requester_id,
            "tautulli_user_id": tautulli_user_id,
            "tautulli_user_name": tautulli_user_name,
        }
    )


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
