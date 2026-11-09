#!/usr/bin/env python3
"""Small formatting helpers shared by the monitors."""

from datetime import datetime, timezone


def fmt_bytes(n: float | int | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= size:
            return f"{n / size:.2f} {unit}" if unit == "GiB" else f"{n / size:.1f} {unit}"
    return f"{n:.0f} B"


def level_icon(percent: float) -> str:
    if percent < 50:
        return "🟢"
    if percent < 80:
        return "🟡"
    return "🔴"


def bar(percent: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, percent))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled) + f" {pct:.0f}%"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def parse_docker_ts(value: str | None) -> datetime | None:
    """Parse Docker's RFC3339 timestamps (nanosecond precision, 'Z' suffix)."""
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_elapsed(seconds: float | None) -> str:
    """Duration with second precision for short spans: 45s, 5m 12s, 2h 03m, 3d 4h."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"
