"""Regression: history rows with no id are skipped, not crash the insert."""

from __future__ import annotations

from pathlib import Path

from dms.db import connect
from dms.migrations import apply_pending
from dms.models import TautulliHistoryRow
from dms.sync.tautulli_sync import _insert_history_row


def test_insert_history_row_skips_when_id_missing(tmp_path: Path) -> None:
    """A row with id=None is silently dropped — source_row_id is NOT NULL."""
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        h = TautulliHistoryRow.model_validate({"id": "", "rating_key": 1})
        assert h.id is None
        # Must not raise. Returns False (nothing inserted).
        inserted = _insert_history_row(c, h)
        assert inserted is False
        n = c.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0]
        assert n == 0
    finally:
        c.close()


def test_insert_history_row_inserts_when_id_present(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        h = TautulliHistoryRow.model_validate({"id": 42, "rating_key": 100, "media_type": "movie"})
        assert _insert_history_row(c, h) is True
        n = c.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0]
        assert n == 1
    finally:
        c.close()
