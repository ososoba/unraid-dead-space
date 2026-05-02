"""Authentication helpers: session cookie + CSRF + login dependency.

Single user, env-driven (`APP_USERNAME` + `APP_PASSWORD_HASH`). The
session is a Starlette SessionMiddleware-managed dict; we add CSRF
protection by storing a token in the session and requiring a matching
form/header value on every state-changing request.

Cookie is signed with `SESSION_SECRET` (raised if absent), expires after
`SESSION_DAYS` days, `HttpOnly` + `SameSite=Lax`, `Secure` per env.
"""

from __future__ import annotations

import logging
import os
import secrets

import bcrypt
from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "dms_user"
SESSION_CSRF_KEY = "dms_csrf"
CSRF_FORM_FIELD = "_csrf"
CSRF_HEADER = "X-CSRF-Token"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def session_secret() -> str:
    """SESSION_SECRET is required. Raise loudly if it's missing or stub-ish."""
    raw = os.environ.get("SESSION_SECRET", "").strip()
    if not raw or len(raw) < 16:
        raise RuntimeError(
            "SESSION_SECRET env var must be set to a random string of at least "
            '16 characters. Generate one with: python -c "import secrets; '
            'print(secrets.token_urlsafe(32))"'
        )
    return raw


def cookie_secure() -> bool:
    return _bool_env("COOKIE_SECURE", default=False)


def session_max_age_seconds() -> int:
    return _int_env("SESSION_DAYS", 90) * 86400


def app_username() -> str:
    return os.environ.get("APP_USERNAME", "").strip()


def app_password_hash() -> bytes:
    raw = os.environ.get("APP_PASSWORD_HASH", "").strip()
    if not raw:
        raise RuntimeError(
            "APP_PASSWORD_HASH not set. Generate with: python -m dms.cli.hash_password"
        )
    return raw.encode("utf-8")


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time username + bcrypt password check."""
    expected_user = app_username()
    if not expected_user:
        logger.warning("APP_USERNAME is empty — login disabled")
        return False
    user_ok = secrets.compare_digest(username.strip(), expected_user)
    pass_ok = False
    try:
        pass_ok = bcrypt.checkpw(password.encode("utf-8"), app_password_hash())
    except (RuntimeError, ValueError) as exc:
        logger.warning("password check failed: %s", exc)
    return user_ok and pass_ok


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_USER_KEY))


def current_user(request: Request) -> str | None:
    user = request.session.get(SESSION_USER_KEY)
    return user if isinstance(user, str) else None


def login(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = username
    # Rotate CSRF on login.
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)


def logout(request: Request) -> None:
    request.session.clear()


def get_or_set_csrf(request: Request) -> str:
    """Return the session CSRF token; mint one if absent."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    """Verify CSRF token from form field or X-CSRF-Token header."""
    expected = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(expected, str) or not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing session CSRF token")

    submitted = request.headers.get(CSRF_HEADER)
    if not submitted:
        # Read from form if no header present.
        try:
            form = await request.form()
        except Exception:
            form = None
        if form is not None:
            submitted = form.get(CSRF_FORM_FIELD)
    if not submitted or not secrets.compare_digest(str(submitted), expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")


def require_login(request: Request) -> str:
    """FastAPI dependency: return current user, raise 401 if unauthenticated."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "login required",
            headers={"Location": "/login"},
        )
    return user
