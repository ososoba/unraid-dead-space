"""Sync run + per-step bookkeeping helpers.

Wraps `sync_jobs` and `sync_run_steps` so the runner can stay focused on
orchestration. Steps support a `step()` context manager that records
running → succeeded/failed transitions automatically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

JobKind = Literal["full", "incremental", "manual"]
JobStatus = Literal["running", "succeeded", "failed", "partial"]
StepStatus = Literal["running", "succeeded", "failed", "skipped"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunStep:
    id: int
    name: str
    items_seen: int = 0
    items_changed: int = 0
    error: str | None = None


@dataclass
class SyncRun:
    id: int
    kind: JobKind
    requested_by: str
    started_at: str
    cursor: dict = field(default_factory=dict)


def start_run(
    conn: sqlite3.Connection,
    *,
    kind: JobKind = "full",
    requested_by: str = "scheduler",
) -> SyncRun:
    started = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO sync_jobs (kind, status, requested_by, started_at) "
            "VALUES (?, 'running', ?, ?)",
            (kind, requested_by, started),
        )
    return SyncRun(
        id=int(cur.lastrowid or 0), kind=kind, requested_by=requested_by, started_at=started
    )


def finish_run(
    conn: sqlite3.Connection,
    run: SyncRun,
    *,
    status: JobStatus,
    error: dict | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status = ?, finished_at = ?, error_json = ? WHERE id = ?",
            (status, _now_iso(), json.dumps(error) if error else None, run.id),
        )


def update_cursor(conn: sqlite3.Connection, run: SyncRun, cursor: dict) -> None:
    """Persist the run cursor for resumable backfills."""
    run.cursor = cursor
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET cursor_json = ? WHERE id = ?",
            (json.dumps(cursor), run.id),
        )


@contextmanager
def step(
    conn: sqlite3.Connection,
    run: SyncRun,
    name: str,
    *,
    swallow_errors: bool = False,
) -> Iterator[RunStep]:
    """Track a single sync step. Records running → succeeded/failed.

    If `swallow_errors=True`, exceptions are caught and recorded as failed
    without re-raising — useful for per-instance pulls where one Arr being
    down shouldn't abort the whole run.
    """
    started = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO sync_run_steps (run_id, step_name, status, started_at) "
            "VALUES (?, ?, 'running', ?)",
            (run.id, name, started),
        )
    step_obj = RunStep(id=int(cur.lastrowid or 0), name=name)
    try:
        yield step_obj
    except Exception as exc:  # noqa: BLE001 — bookkeeping must catch everything
        _finalize_step(conn, step_obj, status="failed", error_json=json.dumps({"error": str(exc)}))
        if not swallow_errors:
            raise
    else:
        _finalize_step(
            conn,
            step_obj,
            status="succeeded",
            error_json=json.dumps({"error": step_obj.error}) if step_obj.error else None,
        )


def _finalize_step(
    conn: sqlite3.Connection,
    step_obj: RunStep,
    *,
    status: StepStatus,
    error_json: str | None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE sync_run_steps "
            "SET status = ?, finished_at = ?, items_seen = ?, items_changed = ?, error_json = ? "
            "WHERE id = ?",
            (
                status,
                _now_iso(),
                step_obj.items_seen,
                step_obj.items_changed,
                error_json,
                step_obj.id,
            ),
        )


def step_results(conn: sqlite3.Connection, run: SyncRun) -> list[sqlite3.Row]:
    """Read all step rows for a run. Used by the runner + UI."""
    return conn.execute(
        "SELECT step_name, status, items_seen, items_changed, error_json "
        "FROM sync_run_steps WHERE run_id = ? ORDER BY id",
        (run.id,),
    ).fetchall()


def overall_status(steps: list[sqlite3.Row]) -> JobStatus:
    """Roll step statuses into a job status."""
    if not steps:
        return "failed"
    statuses = {row["status"] for row in steps}
    if "failed" in statuses and statuses != {"failed"}:
        return "partial"
    if statuses == {"failed"}:
        return "failed"
    return "succeeded"
