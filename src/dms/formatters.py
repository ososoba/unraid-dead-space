"""Tiny human-friendly formatters used in templates.

Registered as Jinja2 filters in app.create_app via _build_templates.
"""

from __future__ import annotations

from datetime import UTC, datetime

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


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
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d")


def relative_days(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
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
