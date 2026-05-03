"""Tiny human-friendly formatters used in templates.

Registered as Jinja2 filters in `app.create_app` via `_build_templates`.

Date formatters resolve the user's timezone from the `TZ` env var
(falling back to UTC). All inbound timestamps are stored as ISO 8601 in
UTC; we convert to local before rendering so a Tautulli play at
2026-01-05 23:00 Toronto time doesn't display as 2026-01-06.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def _local_tz() -> ZoneInfo:
    """Resolve the configured display timezone, with a UTC fallback."""
    name = (os.environ.get("TZ") or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown TZ %r — falling back to UTC", name)
        return ZoneInfo("UTC")


def _to_local(value: str) -> datetime | None:
    """Parse an ISO 8601 string and convert to the configured local TZ."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_local_tz())


def humansize(value: int | float | None) -> str:
    if value is None:
        return "—"
    n = float(value)
    unit_idx = 0
    while n >= 1024 and unit_idx < len(_BYTE_UNITS) - 1:
        n /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(n)} B"
    return f"{n:.1f} {_BYTE_UNITS[unit_idx]}"


def humandate(value: str | None) -> str:
    if not value:
        return "—"
    dt = _to_local(value)
    if dt is None:
        return value
    return dt.strftime("%Y-%m-%d")


def relative_days(value: str | None) -> str:
    if not value:
        return "—"
    dt = _to_local(value)
    if dt is None:
        return value
    delta = datetime.now(_local_tz()) - dt
    days = int(delta.total_seconds() // 86400)
    if days <= 0:
        return "today"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.0f}%"
