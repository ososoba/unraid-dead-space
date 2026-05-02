"""Shared test fixtures.

`reset_login_throttle` runs before every test so accumulated attempts in
the in-memory throttle don't leak between tests.
"""

from __future__ import annotations

import pytest


def _csrf_from_html(html: str) -> str:
    marker = 'name="_csrf" value="'
    idx = html.find(marker)
    if idx < 0:
        return ""
    start = idx + len(marker)
    return html[start : html.find('"', start)]


def login_with_csrf(client, username: str, password: str):
    """Helper: GET /login (mints CSRF), then POST /login with the token + creds."""
    page = client.get("/login")
    csrf = _csrf_from_html(page.text)
    return client.post(
        "/login",
        data={"_csrf": csrf, "username": username, "password": password},
        follow_redirects=False,
    )


@pytest.fixture(autouse=True)
def reset_login_throttle():
    """Wipe the per-process login throttle bucket between tests so a flurry
    of intentional bad-password tests doesn't trip the lockout."""
    from dms.routes import login

    login._attempts.clear()
    login._lockouts.clear()
    yield
    login._attempts.clear()
    login._lockouts.clear()
