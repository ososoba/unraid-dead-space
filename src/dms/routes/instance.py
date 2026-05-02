"""Per-instance deepdive: Never Watched / Stale / Orphans tabs.

Querystring controls:
  ?tab=never|stale|orphans       (default: never)
  ?scope=anyone|requester        (default: anyone)
  ?state=all|finished|partial    (Stale tab only, default: all)
  ?sort=size|added|last_played|coverage  (default: size)
  ?page=1&per_page=50            (default: 1, 50)
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from dms import auth
from dms.deps import get_db
from dms.views import candidates as candidates_view

router = APIRouter()

VALID_TABS = ("never", "stale", "orphans")
VALID_SCOPES = ("anyone", "requester")
VALID_STATES = ("all", "finished", "partial")
VALID_SORTS = ("size", "added", "last_played", "coverage")


@router.get("/instance/{slug}", include_in_schema=False, response_class=HTMLResponse)
async def instance_deepdive(
    slug: str,
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    qs = request.query_params
    tab = qs.get("tab", "never")
    scope = qs.get("scope", "anyone")
    state = qs.get("state", "all")
    sort = qs.get("sort", "size")
    if tab not in VALID_TABS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad tab {tab!r}")
    if scope not in VALID_SCOPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad scope {scope!r}")
    if state not in VALID_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad state {state!r}")
    if sort not in VALID_SORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad sort {sort!r}")

    page = max(1, int(qs.get("page", "1") or "1"))
    per_page = min(200, max(10, int(qs.get("per_page", "50") or "50")))

    inst = conn.execute(
        "SELECT id, slug, kind, name FROM instances WHERE slug = ?", (slug,)
    ).fetchone()
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown instance {slug!r}")

    run = candidates_view.latest_run(conn)
    rows: list = []
    total = 0
    if run is not None:
        reasons = candidates_view.reasons_for_tab(tab, scope=scope, state=state)
        rows, total = candidates_view.list_candidates(
            conn,
            run_id=run.id,
            reasons=reasons,
            instance_slug=None if tab == "orphans" else slug,
            sort=sort,
            page=page,
            per_page=per_page,
        )
    pages = max(1, -(-total // per_page))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "instance.html",
        {
            "user": user,
            "instance": inst,
            "tab": tab,
            "scope": scope,
            "state": state,
            "sort": sort,
            "page": page,
            "per_page": per_page,
            "rows": rows,
            "total": total,
            "pages": pages,
            "run": run,
            "reason_label": candidates_view.reason_label,
        },
    )
