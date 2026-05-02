"""Top-level sync orchestrator.

Owns the lock lifecycle, sync_jobs row, and per-step bookkeeping. Calls
each step in order, swallowing per-instance failures so one Arr or one
Overseerr being down does not abort the entire run.

Usage:
    from dms.sync.runner import run_sync
    summary = await run_sync(conn, config, requested_by="manual")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from dms.config import AppConfig
from dms.sync import (
    arr_sync,
    attribution,
    candidates_db,
    locks,
    plex_sync,
    requester_sync,
    runs,
    tautulli_sync,
    watch_state,
)
from dms.sync.runs import JobKind

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    run_id: int
    status: str
    arr_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    plex_items_seen: int = 0
    tautulli_history_inserted: int = 0
    requester_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    attribution_rows: int = 0
    watch_state_rows: int = 0
    candidate_rows: int = 0


async def run_sync(
    conn,
    config: AppConfig,
    *,
    kind: JobKind = "manual",
    requested_by: str = "scheduler",
    sync_lock_ttl_minutes: int = 120,
) -> SyncSummary:
    handle = locks.acquire(
        conn,
        ttl_minutes=sync_lock_ttl_minutes,
        owner=f"{requested_by}-{__import__('os').getpid()}",
    )
    run = runs.start_run(conn, kind=kind, requested_by=requested_by)
    summary = SyncSummary(run_id=run.id, status="running")

    try:
        # Per-Arr-instance pulls (swallow per-instance errors to allow partial).
        for inst in config.arr_instances:
            with runs.step(conn, run, f"arr:{inst.slug}", swallow_errors=True) as st:
                result = await arr_sync.sync_arr_instance(
                    conn,
                    inst,
                    run_id=run.id,
                    step=st,
                    timeout=config.http.timeout_seconds,
                )
                summary.arr_results[inst.slug] = {
                    "items_seen": result.items_seen,
                    "files_seen": result.files_seen,
                    "episodes_seen": result.episodes_seen,
                }
            locks.heartbeat(conn, handle)

        # Plex inventory + Tautulli history.
        if config.tautulli is not None:
            with runs.step(conn, run, "plex_inventory", swallow_errors=True) as st:
                pres = await plex_sync.sync_plex_inventory(
                    conn,
                    config.tautulli,
                    run_id=run.id,
                    step=st,
                    concurrency=config.http.max_concurrency,
                    timeout=config.http.timeout_seconds,
                )
                summary.plex_items_seen = pres.items_seen
            locks.heartbeat(conn, handle)

            with runs.step(conn, run, "tautulli_history", swallow_errors=True) as st:
                tres = await tautulli_sync.sync_tautulli_history(
                    conn,
                    run,
                    config.tautulli,
                    step=st,
                    page_size=config.http.backfill_page_size,
                    timeout=config.http.timeout_seconds,
                )
                summary.tautulli_history_inserted = tres.rows_inserted
            locks.heartbeat(conn, handle)

        # Requesters (per instance), then user identity map refresh.
        requester_users_by_source: dict[str, list] = {}
        for inst in config.requester_instances:
            with runs.step(conn, run, f"requester:{inst.slug}", swallow_errors=True) as st:
                res, users = await requester_sync.sync_requester_instance(
                    conn,
                    inst,
                    step=st,
                    timeout=config.http.timeout_seconds,
                )
                summary.requester_results[inst.slug] = {
                    "request_count": res.request_count,
                    "user_count": res.user_count,
                }
                requester_users_by_source.setdefault(inst.source, []).extend(users)
            locks.heartbeat(conn, handle)

        if config.tautulli is not None and requester_users_by_source:
            with runs.step(conn, run, "user_identity_map", swallow_errors=True):
                taut_users = await tautulli_sync.fetch_tautulli_users(
                    config.tautulli,
                    timeout=config.http.timeout_seconds,
                )
                requester_sync.refresh_user_identity_map(
                    conn,
                    requester_users_by_source,
                    taut_users,
                )

        # Derived computations — these MUST succeed for the run to be useful.
        with runs.step(conn, run, "request_attribution") as st:
            summary.attribution_rows = attribution.recompute_request_attribution(conn)
            st.items_changed = summary.attribution_rows

        with runs.step(conn, run, "watch_state") as st:
            summary.watch_state_rows = watch_state.recompute_watch_state(
                conn,
                threshold_movies_pct=config.watch.threshold_movies_pct,
                threshold_episodes_pct=config.watch.threshold_episodes_pct,
                specials_mode=config.watch.specials_mode,
            )
            st.items_changed = summary.watch_state_rows

        with runs.step(conn, run, "candidates") as st:
            summary.candidate_rows = candidates_db.compute_candidates(
                conn,
                run_id=run.id,
                never_watched_days=config.watch.never_watched_days,
                stale_days=config.watch.stale_days,
            )
            st.items_changed = summary.candidate_rows

    except Exception as exc:  # noqa: BLE001 — bookkeeping requires it
        logger.exception("sync run %d failed", run.id)
        runs.finish_run(conn, run, status="failed", error={"error": str(exc)})
        summary.status = "failed"
        raise
    finally:
        locks.release(conn, handle)

    # Final status from per-step results.
    step_rows = runs.step_results(conn, run)
    final = runs.overall_status(step_rows)
    runs.finish_run(conn, run, status=final)
    summary.status = final
    return summary
