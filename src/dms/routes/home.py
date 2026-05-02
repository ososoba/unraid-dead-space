"""Homepage: summary cards, age buckets, last sync, partial-failure banner."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from dms import auth
from dms.deps import get_db
from dms.views import candidates as candidates_view
from dms.views import summary

router = APIRouter()


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def home(
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    templates = request.app.state.templates
    run = candidates_view.latest_run(conn)
    if run is None:
        return templates.TemplateResponse(
            request,
            "home_empty.html",
            {"user": user},
        )
    items, total_bytes = summary.headline_reclaim_bytes(conn, run.id)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "run": run,
            "headline_items": items,
            "headline_bytes": total_bytes,
            "reasons": summary.reason_summary(conn, run.id),
            "age_buckets": summary.age_buckets_for_never_watched(conn, run.id),
            "instances": summary.instance_cards(conn),
            "failed_steps": summary.failed_steps(conn, run.id),
            "reason_label": candidates_view.reason_label,
        },
    )
