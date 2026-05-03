-- Track the dashboard's headline + per-reason totals over time so the
-- homepage can show a trend sparkline and per-card "since last sync"
-- delta indicators. One row per (sync_run, reason); the special
-- `reason='TOTAL'` row holds the dedup-by-arr-item headline number
-- shown at the top of the dashboard.

CREATE TABLE IF NOT EXISTS dashboard_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_run_id  INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  taken_at     TEXT NOT NULL DEFAULT (datetime('now')),
  reason       TEXT NOT NULL,
  item_count   INTEGER NOT NULL DEFAULT 0,
  total_bytes  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (sync_run_id, reason)
);

CREATE INDEX IF NOT EXISTS idx_dashboard_snapshots_taken
  ON dashboard_snapshots(taken_at);
CREATE INDEX IF NOT EXISTS idx_dashboard_snapshots_reason
  ON dashboard_snapshots(reason, id);
