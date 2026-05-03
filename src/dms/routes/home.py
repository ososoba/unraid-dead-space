"""Homepage: summary cards, age buckets, last sync, partial-failure banner."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from dms import auth
from dms.deps import get_db
from dms.sync.snapshots import TOTAL_KEY
from dms.views import candidates as candidates_view
from dms.views import snapshots as snapshots_view
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
    reasons = summary.reason_summary(conn, run.id)
    # Snapshot lookups for the trend strip + per-card "since last sync"
    # deltas. The headline uses the special TOTAL_KEY (DISTINCT-arr-item /
    # MAX-size dedup); per-reason cards key on the reason itself.
    headline_stat = snapshots_view.latest_with_delta(conn, TOTAL_KEY)
    headline_series = snapshots_view.series(conn, TOTAL_KEY, limit=30)
    reason_stats = snapshots_view.latest_with_delta_many(conn, [r.reason for r in reasons])
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "run": run,
            "headline_items": items,
            "headline_bytes": total_bytes,
            "headline_stat": headline_stat,
            "headline_series": headline_series,
            "reasons": reasons,
            "reason_stats": reason_stats,
            "age_buckets": summary.age_buckets_for_never_watched(conn, run.id),
            "instances": summary.instance_cards(conn),
            "failed_steps": summary.failed_steps(conn, run.id),
            "top_requesters": summary.top_requesters_by_reclaim(conn, run.id, limit=5),
            "age_bucket_defs": summary.AGE_BUCKETS,
            # Cards have plenty of horizontal room — use the verbose label
            # so the dashboard reads cleanly. The compact label is for table
            # rows where space is tight.
            "reason_label": candidates_view.reason_long_label,
        },
    )
