"""Ignored items list + unignore action."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dms import auth
from dms.deps import get_db

router = APIRouter()


@router.get("/ignored", include_in_schema=False, response_class=HTMLResponse)
async def ignored_page(
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    rows = conn.execute(
        """
        SELECT ai.id, ai.title, ai.year, ai.kind, i.slug, i.name AS instance_name,
               COALESCE(SUM(af.size_bytes), 0) AS size_bytes,
               ra.requester_name
        FROM arr_items ai
        JOIN instances i ON i.id = ai.instance_id
        LEFT JOIN arr_files af ON af.arr_item_id = ai.id AND af.deleted_at IS NULL
        LEFT JOIN request_attribution ra ON ra.arr_item_id = ai.id
        WHERE ai.ignored_local = 1 AND ai.deleted_at IS NULL
        GROUP BY ai.id
        ORDER BY size_bytes DESC
        """
    ).fetchall()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "ignored.html",
        {"user": user, "rows": rows},
    )


@router.post("/items/{arr_item_id}/ignore", include_in_schema=False)
async def ignore_item(
    arr_item_id: int,
    request: Request,
    user: str = Depends(auth.require_login),
    _: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    row = conn.execute(
        "SELECT id, title FROM arr_items WHERE id = ? AND deleted_at IS NULL",
        (arr_item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "arr_item not found")
    with conn:
        conn.execute(
            "UPDATE arr_items SET ignored_local = 1 WHERE id = ?",
            (arr_item_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'ignore', ?, ?)",
            (user, str(arr_item_id), json.dumps({"title": row["title"]})),
        )
    return JSONResponse({"ok": True})


@router.post("/items/{arr_item_id}/unignore", include_in_schema=False)
async def unignore_item(
    arr_item_id: int,
    request: Request,
    user: str = Depends(auth.require_login),
    _: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    row = conn.execute(
        "SELECT id, title FROM arr_items WHERE id = ?",
        (arr_item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "arr_item not found")
    with conn:
        conn.execute(
            "UPDATE arr_items SET ignored_local = 0 WHERE id = ?",
            (arr_item_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details_json) "
            "VALUES (?, 'unignore', ?, ?)",
            (user, str(arr_item_id), json.dumps({"title": row["title"]})),
        )
    return JSONResponse({"ok": True})
