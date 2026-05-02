"""DB-backed app settings (config table) merged with env defaults.

Read precedence: DB row > env > hardcoded default. Writers (config page)
upsert into the `config` table; readers ask `get_setting` which checks
the DB first.

Only the user-tunable subset of env vars is exposed here — secrets like
SESSION_SECRET / APP_PASSWORD_HASH are env-only and never DB-stored.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from dms.config import WatchConfig

# Keys this module knows how to read/write.
TUNABLE_KEYS: tuple[str, ...] = (
    "WATCH_SCOPE",
    "WATCH_THRESHOLD_MOVIES_PCT",
    "WATCH_THRESHOLD_EPISODES_PCT",
    "SERIES_SPECIALS_MODE",
    "NEVER_WATCHED_DAYS",
    "STALE_DAYS",
)


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is not None and row["value"] is not None:
        return str(row["value"])
    return os.environ.get(key, default)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in TUNABLE_KEYS:
        raise ValueError(f"refusing to set non-tunable key {key!r}")
    with conn:
        conn.execute(
            """
            INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value),
        )


def all_tunables(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {k: get_setting(conn, k) for k in TUNABLE_KEYS}


@dataclass
class EffectiveWatchConfig:
    scope: str
    threshold_movies_pct: int
    threshold_episodes_pct: int
    specials_mode: str
    never_watched_days: int
    stale_days: int

    def to_dataclass(self) -> WatchConfig:
        return WatchConfig(
            scope=self.scope,  # type: ignore[arg-type]
            threshold_movies_pct=self.threshold_movies_pct,
            threshold_episodes_pct=self.threshold_episodes_pct,
            specials_mode=self.specials_mode,  # type: ignore[arg-type]
            never_watched_days=self.never_watched_days,
            stale_days=self.stale_days,
        )


def effective_watch_config(conn: sqlite3.Connection) -> EffectiveWatchConfig:
    def _int(key: str, default: int) -> int:
        v = get_setting(conn, key)
        try:
            return int(v) if v else default
        except ValueError:
            return default

    return EffectiveWatchConfig(
        scope=(get_setting(conn, "WATCH_SCOPE") or "anyone").lower(),
        threshold_movies_pct=_int("WATCH_THRESHOLD_MOVIES_PCT", 80),
        threshold_episodes_pct=_int("WATCH_THRESHOLD_EPISODES_PCT", 80),
        specials_mode=(get_setting(conn, "SERIES_SPECIALS_MODE") or "ignore").lower(),
        never_watched_days=_int("NEVER_WATCHED_DAYS", 90),
        stale_days=_int("STALE_DAYS", 180),
    )


def validate_setting(key: str, value: str) -> None:
    """Raise ValueError on bad inputs. Used by the config save endpoint."""
    if key in {"WATCH_THRESHOLD_MOVIES_PCT", "WATCH_THRESHOLD_EPISODES_PCT"}:
        try:
            i = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be integer 1..100") from exc
        if not (1 <= i <= 100):
            raise ValueError(f"{key} must be 1..100")
    elif key in {"NEVER_WATCHED_DAYS", "STALE_DAYS"}:
        try:
            i = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a non-negative integer") from exc
        if i < 0:
            raise ValueError(f"{key} must be >= 0")
    elif key == "WATCH_SCOPE":
        if value not in {"anyone", "requester"}:
            raise ValueError("WATCH_SCOPE must be 'anyone' or 'requester'")
    elif key == "SERIES_SPECIALS_MODE":
        if value not in {"ignore", "include"}:
            raise ValueError("SERIES_SPECIALS_MODE must be 'ignore' or 'include'")
    else:
        raise ValueError(f"unknown setting {key!r}")


@dataclass
class InstanceStatus:
    slug: str
    kind: str
    name: str
    url: str
    last_seen_ok_at: str | None
    last_error: str | None
    item_count: int = 0


def list_instance_status(conn: sqlite3.Connection) -> list[InstanceStatus]:
    rows = conn.execute(
        """
        SELECT i.slug, i.kind, i.name, i.url, i.last_seen_ok_at, i.last_error,
               COUNT(ai.id) AS n
        FROM instances i
        LEFT JOIN arr_items ai ON ai.instance_id = i.id AND ai.deleted_at IS NULL
        GROUP BY i.id
        ORDER BY i.kind, i.slug
        """
    ).fetchall()
    return [
        InstanceStatus(
            slug=r["slug"],
            kind=r["kind"],
            name=r["name"],
            url=r["url"],
            last_seen_ok_at=r["last_seen_ok_at"],
            last_error=r["last_error"],
            item_count=r["n"],
        )
        for r in rows
    ]


# Helper used by the requester ↔ Plex user mapping section in /config.
@dataclass
class UserMappingRow:
    id: int
    requester_source: str
    requester_id: int | None
    requester_name: str | None
    tautulli_user_id: int | None
    tautulli_user_name: str | None
    match_method: str
    confidence: str


def list_user_mappings(conn: sqlite3.Connection) -> list[UserMappingRow]:
    rows = conn.execute(
        "SELECT * FROM user_identity_map ORDER BY requester_source, requester_name"
    ).fetchall()
    return [
        UserMappingRow(
            **{
                k: r[k]
                for k in (
                    "id",
                    "requester_source",
                    "requester_id",
                    "requester_name",
                    "tautulli_user_id",
                    "tautulli_user_name",
                    "match_method",
                    "confidence",
                )
            }
        )
        for r in rows
    ]


def list_tautulli_users(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """For dropdowns: distinct (user_id, name) tuples seen in watch_events."""
    rows = conn.execute(
        "SELECT DISTINCT user_id, user_name FROM watch_events "
        "WHERE user_id IS NOT NULL ORDER BY user_name"
    ).fetchall()
    return [(int(r["user_id"]), str(r["user_name"] or "")) for r in rows]


def filter_known(keys: Iterable[str]) -> list[str]:
    return [k for k in keys if k in TUNABLE_KEYS]


def save_settings(conn: sqlite3.Connection, items: dict[str, str]) -> dict[str, str]:
    """Validate and save in one shot. Returns the saved subset."""
    saved: dict[str, str] = {}
    for k, v in items.items():
        if k not in TUNABLE_KEYS:
            continue
        validate_setting(k, v)
        set_setting(conn, k, v)
        saved[k] = v
    return saved


# Mark as "unused" for ruff to leave the dataclass field in place.
_ = field
