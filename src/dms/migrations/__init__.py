"""Migration runner for DMS.

Migrations are SQL files in this package, named `NNNN_<slug>.sql` where NNNN
is a zero-padded version number (e.g. `0001_initial.sql`). The runner applies
pending migrations in version order and records what was applied in a
`schema_version` table.

Each migration is idempotent (relies on `IF NOT EXISTS` clauses) so a partial
failure followed by a retry is safe — though a successful migration is
recorded only after the SQL completes, which is the common path.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

logger = logging.getLogger(__name__)

_FILENAME_RX = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$", re.IGNORECASE)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT
        )
        """
    )
    conn.commit()


def list_migrations() -> list[Migration]:
    """Discover all `NNNN_*.sql` files shipped with the package."""
    migrations: list[Migration] = []
    package = resources.files(__name__)
    for entry in package.iterdir():
        match = _FILENAME_RX.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        name = match.group(2)
        sql = entry.read_text(encoding="utf-8")
        migrations.append(Migration(version=version, name=name, sql=sql))
    migrations.sort(key=lambda m: m.version)
    return migrations


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_schema_version_table(conn)
    cur = conn.execute("SELECT version FROM schema_version")
    return {row[0] for row in cur.fetchall()}


def apply_pending(conn: sqlite3.Connection) -> list[Migration]:
    """Apply every migration not yet recorded. Returns those applied this call."""
    _ensure_schema_version_table(conn)
    already = applied_versions(conn)
    applied_now: list[Migration] = []
    for migration in list_migrations():
        if migration.version in already:
            continue
        logger.info("applying migration %04d_%s", migration.version, migration.name)
        # `executescript` issues an implicit COMMIT before/after; we cannot
        # wrap it in BEGIN. The migration SQL uses `IF NOT EXISTS` clauses
        # so a re-run after partial failure is safe.
        conn.executescript(migration.sql)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        conn.commit()
        applied_now.append(migration)
    return applied_now
