"""sync_runs: start_run, step context manager, status rollup."""

from __future__ import annotations

from pathlib import Path

import pytest

from dms.db import connect
from dms.migrations import apply_pending
from dms.sync.runs import (
    finish_run,
    overall_status,
    start_run,
    step,
    step_results,
    update_cursor,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "db.sqlite")
    apply_pending(c)
    yield c
    c.close()


def test_start_run_creates_running_row(conn) -> None:
    run = start_run(conn, kind="manual", requested_by="test")
    row = conn.execute("SELECT kind, status, requested_by FROM sync_jobs").fetchone()
    assert row["kind"] == "manual"
    assert row["status"] == "running"
    assert row["requested_by"] == "test"
    assert run.id == 1


def test_step_context_records_succeeded(conn) -> None:
    run = start_run(conn)
    with step(conn, run, "test_step") as s:
        s.items_seen = 5
        s.items_changed = 3
    rows = step_results(conn, run)
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["items_seen"] == 5
    assert rows[0]["items_changed"] == 3


def test_step_context_records_failed_and_reraises(conn) -> None:
    run = start_run(conn)
    with pytest.raises(RuntimeError, match="boom"), step(conn, run, "broken_step"):
        raise RuntimeError("boom")
    rows = step_results(conn, run)
    assert rows[0]["status"] == "failed"
    assert "boom" in rows[0]["error_json"]


def test_step_swallow_errors(conn) -> None:
    run = start_run(conn)
    # Should NOT raise.
    with step(conn, run, "flaky", swallow_errors=True):
        raise RuntimeError("network timeout")
    rows = step_results(conn, run)
    assert rows[0]["status"] == "failed"


def test_overall_status_partial_when_mixed(conn) -> None:
    run = start_run(conn)
    with step(conn, run, "ok_step"):
        pass
    with step(conn, run, "broken", swallow_errors=True):
        raise RuntimeError("nope")
    rows = step_results(conn, run)
    assert overall_status(rows) == "partial"


def test_overall_status_succeeded_when_all_ok(conn) -> None:
    run = start_run(conn)
    with step(conn, run, "a"):
        pass
    with step(conn, run, "b"):
        pass
    assert overall_status(step_results(conn, run)) == "succeeded"


def test_overall_status_failed_when_all_failed(conn) -> None:
    run = start_run(conn)
    with step(conn, run, "a", swallow_errors=True):
        raise RuntimeError("x")
    with step(conn, run, "b", swallow_errors=True):
        raise RuntimeError("y")
    assert overall_status(step_results(conn, run)) == "failed"


def test_update_cursor_persists(conn) -> None:
    run = start_run(conn)
    update_cursor(conn, run, {"tautulli_max_row_id": 1234})
    row = conn.execute("SELECT cursor_json FROM sync_jobs WHERE id = ?", (run.id,)).fetchone()
    assert "1234" in row["cursor_json"]


def test_finish_run_sets_finished_at(conn) -> None:
    run = start_run(conn)
    finish_run(conn, run, status="succeeded")
    row = conn.execute(
        "SELECT status, finished_at FROM sync_jobs WHERE id = ?", (run.id,)
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None
