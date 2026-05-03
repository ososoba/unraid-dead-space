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


def signed_humansize(value: int | float | None) -> str:
    """Same as `humansize` but with an explicit +/− sign for deltas. Zero
    renders as a plain "0 B" (no sign — nothing changed)."""
    if value is None:
        return "—"
    n = int(value)
    if n == 0:
        return "0 B"
    sign = "+" if n > 0 else "−"
    return f"{sign}{humansize(abs(n))}"


def signed_pct(value: float | None) -> str:
    """Percentage with explicit +/− sign for deltas; "—" when None."""
    if value is None:
        return "—"
    n = float(value)
    if n == 0:
        return "0%"
    sign = "+" if n > 0 else "−"
    return f"{sign}{abs(n):.1f}%"


def sparkline(
    points: list,
    *,
    width: int = 280,
    height: int = 50,
    attr: str = "total_bytes",
) -> str:
    """Inline-SVG sparkline from a list of objects with a numeric attribute
    (defaults to `total_bytes`, matching `views.snapshots.SnapshotPoint`).

    Returns an empty string when there's nothing meaningful to draw (<2
    points). The path uses `currentColor` so the line inherits text color
    in both light + dark mode."""
    if not points or len(points) < 2:
        return ""
    values = [float(getattr(p, attr)) for p in points]
    vmin = min(values)
    vmax = max(values)
    vrange = max(1.0, vmax - vmin)
    n = len(values)
    pad = 2  # keeps the stroke from clipping at the edges
    coords: list[str] = []
    for i, v in enumerate(values):
        x = pad + (i / (n - 1)) * (width - 2 * pad)
        y = pad + (1 - (v - vmin) / vrange) * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    last_x, last_y = coords[-1].split(",")
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'class="sparkline" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" '
        f'points="{" ".join(coords)}" />'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="currentColor" />'
        f"</svg>"
    )
