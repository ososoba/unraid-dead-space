"""FastAPI app factory.

Creates the app, attaches middleware (ProxyHeaders for Cloudflared,
SessionMiddleware for signed cookies), wires routes, and ensures DB
migrations are applied at startup.

Per-request DB connections live on `request.state.db` via a small
dependency. The connection is opened lazily on first access and closed
in the response phase.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from os import PathLike

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dms import auth, formatters
from dms.db import DEFAULT_DB_PATH, connect
from dms.migrations import apply_pending
from dms.routes import config_route, healthz, home, ignored, instance, login, requesters

logger = logging.getLogger(__name__)


def _build_templates(package_dir: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=f"{package_dir}/templates")
    templates.env.globals["csrf_token"] = lambda request: auth.get_or_set_csrf(request)
    templates.env.filters["humansize"] = formatters.humansize
    templates.env.filters["humandate"] = formatters.humandate
    templates.env.filters["relative_days"] = formatters.relative_days
    templates.env.filters["percent"] = formatters.percent
    return templates


def create_app(
    *,
    db_path: str | PathLike[str] | None = None,
    apply_migrations: bool = True,
) -> FastAPI:
    """Build the FastAPI app. Tests pass a tmp_path DB; runtime uses default
    (or DMS_DB_PATH env var if set by the serve CLI)."""
    if db_path is None:
        import os
        db_path = os.environ.get("DMS_DB_PATH") or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if apply_migrations:
            conn = connect(db_path)
            try:
                apply_pending(conn)
            finally:
                conn.close()
        yield

    app = FastAPI(
        title="Dead Movies & Shows",
        docs_url=None,  # private app — no /docs swagger
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.db_path = db_path

    # Sessions: signed cookie holds {dms_user, dms_csrf, ...}
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        session_cookie="dms_session",
        max_age=auth.session_max_age_seconds(),
        same_site="lax",
        https_only=auth.cookie_secure(),
    )

    # Static + templates rooted in the package directory.
    package_dir = str(__import__("pathlib").Path(__file__).parent)
    app.mount("/static", StaticFiles(directory=f"{package_dir}/static"), name="static")
    templates = _build_templates(package_dir)
    app.state.templates = templates

    # Routes.
    app.include_router(healthz.router)
    app.include_router(login.router)
    app.include_router(config_route.router)
    app.include_router(home.router)  # `/`
    app.include_router(instance.router)  # `/instance/{slug}`
    app.include_router(requesters.router)  # `/requesters`
    app.include_router(ignored.router)  # `/ignored` + `/items/.../ignore`

    return app
