"""Login + logout routes.

Login is CSRF-protected (the form mints a token; the POST verifies it).
Failed attempts are throttled in-memory per source IP — five strikes
inside `WINDOW` triggers a `LOCKOUT` cooldown. The throttle is intentionally
in-process (resets on container restart) — for a tunnel-exposed single-user
app this is enough; persistent ban-lists belong in fail2ban / Cloudflare.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dms import auth
from dms.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- Login throttle ----------

_THROTTLE_WINDOW_SEC = 5 * 60
_THROTTLE_MAX_ATTEMPTS = 5
_THROTTLE_LOCKOUT_SEC = 15 * 60
_attempts: dict[str, deque[float]] = defaultdict(deque)
_lockouts: dict[str, float] = {}
_throttle_lock = Lock()


def _client_id(request: Request) -> str:
    """Cheap client identifier for throttling.

    Uses `request.client.host` only. Uvicorn's ProxyHeadersMiddleware
    replaces this from `X-Forwarded-For` *if and only if* the connecting
    socket peer is in `forwarded_allow_ips`. Reading the raw header here
    would let any direct LAN client spoof the value to dodge the lockout.
    """
    client = request.client
    return client.host if client else "unknown"


def _check_throttle(cid: str) -> float | None:
    """Return seconds-until-allowed if locked out, else None."""
    now = time.monotonic()
    with _throttle_lock:
        until = _lockouts.get(cid)
        if until is not None:
            if until > now:
                return until - now
            del _lockouts[cid]
    return None


def _record_failure(cid: str) -> None:
    now = time.monotonic()
    with _throttle_lock:
        bucket = _attempts[cid]
        bucket.append(now)
        while bucket and bucket[0] < now - _THROTTLE_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= _THROTTLE_MAX_ATTEMPTS:
            _lockouts[cid] = now + _THROTTLE_LOCKOUT_SEC
            bucket.clear()


def _record_success(cid: str) -> None:
    with _throttle_lock:
        _attempts.pop(cid, None)
        _lockouts.pop(cid, None)


# ---------- Routes ----------


@router.get("/login", include_in_schema=False, response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if auth.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    # Mint a CSRF token now so the form can include it.
    auth.get_or_set_csrf(request)
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
    cid = _client_id(request)
    cooldown = _check_throttle(cid)
    if cooldown is not None:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, details_json) "
                "VALUES (?, 'login_fail', ?, ?)",
                (cid[:64], "/login", json.dumps({"reason": "throttled"})),
            )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": f"Too many attempts — try again in {int(cooldown)}s."},
            status_code=429,
        )

    # CSRF verification on the login POST itself (per PLAN.md security).
    try:
        await auth.require_csrf(request)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Form expired — please reload and try again."},
                status_code=403,
            )
        raise

    if not auth.verify_credentials(username, password):
        _record_failure(cid)
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

    _record_success(cid)
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
