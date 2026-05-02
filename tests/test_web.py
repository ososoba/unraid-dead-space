"""HTTP integration tests for the FastAPI app.

Spins up a TestClient against an isolated tmp DB. Auth env vars are set
in conftest fixtures so login works against a known credential.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("SESSION_DAYS", "7")


@pytest.fixture
def client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(db_path=tmp_path / "dms.sqlite")
    with TestClient(app) as c:
        yield c


# ---------- /healthz ----------


def test_healthz_no_auth(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert 1 in body["schema_versions"]


# ---------- Auth ----------


def test_root_requires_auth_when_anonymous(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    # require_login raises 401; redirect-to-login is the UX layer's job.
    assert r.status_code == 401


def test_login_form_renders(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert b"Sign in" in r.content


def test_login_rejects_bad_password(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert b"Invalid" in r.content


def test_login_redirects_on_success(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # cookie was set
    assert "dms_session" in r.cookies or any(c.name == "dms_session" for c in client.cookies.jar)


def test_authenticated_root_renders_dashboard(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = client.get("/")
    assert r.status_code == 200
    # Empty-state homepage on a fresh DB.
    assert b"No syncs yet" in r.content


def test_config_requires_login(client: TestClient) -> None:
    r = client.get("/config", follow_redirects=False)
    assert r.status_code == 401


def test_config_renders_when_authenticated(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = client.get("/config")
    assert r.status_code == 200
    assert b"Configuration" in r.content
    assert b"Thresholds" in r.content


# ---------- CSRF ----------


def test_logout_requires_csrf(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    # No CSRF token submitted.
    r = client.post("/logout")
    assert r.status_code == 403


def test_logout_with_csrf_clears_session(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    # Mint CSRF token by visiting /config (which renders the token).
    page = client.get("/config")
    csrf = _extract_csrf(page.text)
    r = client.post("/logout", data={"_csrf": csrf}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_save_config_requires_csrf(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = client.post("/config/save", data={"NEVER_WATCHED_DAYS": "60"})
    assert r.status_code == 403


def test_save_config_persists(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    page = client.get("/config")
    csrf = _extract_csrf(page.text)
    r = client.post(
        "/config/save",
        data={
            "_csrf": csrf,
            "NEVER_WATCHED_DAYS": "120",
            "STALE_DAYS": "200",
            "WATCH_SCOPE": "requester",
            "SERIES_SPECIALS_MODE": "ignore",
            "WATCH_THRESHOLD_MOVIES_PCT": "85",
            "WATCH_THRESHOLD_EPISODES_PCT": "85",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Round-trip via the page (showing saved values).
    page2 = client.get("/config")
    assert b'value="120"' in page2.content
    assert b'value="200"' in page2.content


def test_save_config_rejects_bad_value(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    page = client.get("/config")
    csrf = _extract_csrf(page.text)
    r = client.post(
        "/config/save",
        data={"_csrf": csrf, "NEVER_WATCHED_DAYS": "not-a-number"},
    )
    assert r.status_code == 400


def test_purge_history_requires_csrf(client: TestClient) -> None:
    client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = client.post("/config/purge-history")
    assert r.status_code == 403


# ---------- helper ----------


def _extract_csrf(html: str) -> str:
    """Pull the first _csrf hidden input value from rendered HTML."""
    marker = 'name="_csrf" value="'
    idx = html.find(marker)
    assert idx >= 0, "no CSRF token in page"
    start = idx + len(marker)
    end = html.find('"', start)
    return html[start:end]
