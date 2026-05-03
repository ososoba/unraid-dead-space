"""Universal candidates view at GET /candidates.

The single drill-down surface — every clickable thing on the dashboard
funnels here with the appropriate querystring. Reuses
`views.candidates.list_candidates` so all filter logic lives in one
place; the route is mostly querystring parsing + template rendering.

Querystring (all optional):
    reason=R[,R,...]     candidate reason(s); omit for "all reasons"
    instance=slug        narrow to one Arr instance
    requester=NAME       narrow to one requester
    age_min=DAYS         only items added at least DAYS ago
    age_max=DAYS         only items added less than DAYS ago
    q=SUBSTRING          case-insensitive title search
    sort=size|added|last_played|coverage   default: size
    page=1, per_page=50
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from dms import auth
from dms.deps import get_db
from dms.views import candidates as candidates_view

router = APIRouter()

VALID_SORTS = ("size", "added", "last_played", "coverage")


def _parse_int(value: str | None, *, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad integer {value!r}") from exc


@router.get("/candidates", include_in_schema=False, response_class=HTMLResponse)
async def candidates_page(
    request: Request,
    user: str = Depends(auth.require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    qs = request.query_params

    # Reasons can be comma-separated or repeated. Validate against the known
    # set so a typo'd querystring returns 400 instead of silently filtering
    # to nothing.
    reasons_raw = qs.get("reason", "").strip()
    reasons: list[str] = []
    if reasons_raw:
        reasons = [r.strip() for r in reasons_raw.split(",") if r.strip()]
        unknown = set(reasons) - set(candidates_view.ALL_REASONS)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"unknown reason(s): {sorted(unknown)}"
            )

    instance_slug = qs.get("instance") or None
    requester = qs.get("requester") or None
    age_min = _parse_int(qs.get("age_min"), default=None)
    age_max = _parse_int(qs.get("age_max"), default=None)
    title_query = (qs.get("q") or "").strip() or None

    sort = qs.get("sort", "size")
    if sort not in VALID_SORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad sort {sort!r}")

    page = max(1, _parse_int(qs.get("page"), default=1) or 1)
    per_page = min(200, max(10, _parse_int(qs.get("per_page"), default=50) or 50))

    run = candidates_view.latest_run(conn)
    rows: list = []
    total = 0
    if run is not None:
        rows, total = candidates_view.list_candidates(
            conn,
            run_id=run.id,
            reasons=reasons or None,
            instance_slug=instance_slug,
            requester_name=requester,
            age_min_days=age_min,
            age_max_days=age_max,
            title_query=title_query,
            sort=sort,  # type: ignore[arg-type]
            page=page,
            per_page=per_page,
        )
    pages = max(1, -(-total // per_page))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "candidates.html",
        {
            "user": user,
            "run": run,
            "rows": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "filter_reasons": reasons,
            "filter_instance": instance_slug,
            "filter_requester": requester,
            "filter_age_min": age_min,
            "filter_age_max": age_max,
            "filter_title_query": title_query or "",
            "filter_sort": sort,
            "all_reasons": candidates_view.ALL_REASONS,
            "reason_label": candidates_view.reason_label,
        },
    )
