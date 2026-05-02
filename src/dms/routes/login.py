"""Login + logout routes."""

from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dms import auth
from dms.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/login", include_in_schema=False, response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if auth.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    templates = request.app.state.templates
    if not auth.verify_credentials(username, password):
        with conn:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, details_json) "
                "VALUES (?, 'login_fail', ?, ?)",
                (username[:64] or "<empty>", "/login", json.dumps({"reason": "bad_credentials"})),
            )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=401,
        )
    auth.login(request, username)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target) VALUES (?, 'login', '/login')",
            (username,),
        )
    return RedirectResponse(url="/", status_code=302)


@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    _: None = Depends(auth.require_csrf),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    user = auth.current_user(request) or "<unknown>"
    auth.logout(request)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target) VALUES (?, 'logout', '/logout')",
            (user,),
        )
    return RedirectResponse(url="/login", status_code=302)
