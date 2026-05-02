"""sync_locks: acquire / heartbeat / release / steal-when-stale."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dms.db import connect
from dms.migrations import apply_pending
from dms.sync.locks import LockHeldError, acquire, heartbeat, release


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "db.sqlite")
    apply_pending(c)
    yield c
    c.close()


def test_acquire_then_release(conn) -> None:
    h = acquire(conn, owner="test-owner")
    assert h.owner == "test-owner"
    rows = conn.execute("SELECT * FROM sync_locks").fetchall()
    assert len(rows) == 1
    assert rows[0]["owner"] == "test-owner"
    assert release(conn, h) is True
    assert conn.execute("SELECT COUNT(*) FROM sync_locks").fetchone()[0] == 0


def test_second_acquire_blocked_when_fresh(conn) -> None:
    h1 = acquire(conn, owner="first")
    with pytest.raises(LockHeldError):
        acquire(conn, owner="second")
    release(conn, h1)


def test_heartbeat_refreshes(conn) -> None:
    h = acquire(conn, owner="me")
    conn.execute("SELECT heartbeat_at FROM sync_locks").fetchone()[0]
    # Manually rewind heartbeat to simulate time passing.
    older = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    conn.execute("UPDATE sync_locks SET heartbeat_at = ?", (older,))
    conn.commit()
    assert heartbeat(conn, h) is True
    after = conn.execute("SELECT heartbeat_at FROM sync_locks").fetchone()[0]
    assert after > older
    release(conn, h)


def test_heartbeat_returns_false_if_stolen(conn) -> None:
    h = acquire(conn, owner="me")
    # Simulate steal: replace owner.
    conn.execute("UPDATE sync_locks SET owner = 'thief'")
    conn.commit()
    assert heartbeat(conn, h) is False


def test_stale_lock_can_be_stolen(conn) -> None:
    h1 = acquire(conn, owner="dead-process")
    # Force heartbeat older than ttl.
    stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    conn.execute("UPDATE sync_locks SET heartbeat_at = ?", (stale,))
    conn.commit()

    h2 = acquire(conn, owner="reaper", ttl_minutes=120)
    assert h2.owner == "reaper"

    # Audit row was written.
    audit = conn.execute(
        "SELECT action, details_json FROM audit_log WHERE action = 'stale_lock_recovered'"
    ).fetchone()
    assert audit is not None
    assert audit["action"] == "stale_lock_recovered"
    # Old handle release should be a no-op (different owner).
    assert release(conn, h1) is False
    release(conn, h2)


def test_release_returns_false_when_lock_already_gone(conn) -> None:
    h = acquire(conn, owner="me")
    conn.execute("DELETE FROM sync_locks")
    conn.commit()
    assert release(conn, h) is False
