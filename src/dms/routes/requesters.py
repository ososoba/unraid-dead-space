"""Requester totals: items + size + reason breakdown per requester."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from dms import auth
from dms.deps import get_db
from dms.views import candidates as candidates_view
from dms.views import summary

router = APIRouter()


@router.get("/requesters", include_in_schema=False, response_class=HTMLResponse)
async def requesters_page(
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    run = candidates_view.latest_run(conn)
    totals = summary.requester_totals(conn, run.id) if run else []
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "requesters.html",
        {"user": user, "run": run, "totals": totals},
    )
