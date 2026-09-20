"""Time-window helpers for Tapo Care list requests."""
from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo


def _parse_now(now_iso: str | None, tz: ZoneInfo) -> datetime:
    if now_iso:
        parsed = datetime.fromisoformat(now_iso)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    return datetime.now(tz)


def build_time_window(days: int, timezone_name: str = "Asia/Tokyo", now_iso: str | None = None) -> tuple[str, str]:
    """Build `[start, end]` strings using local-day boundaries.

    The end is tomorrow's midnight so today's partial clips are included; `days=1`
    asks for yesterday + today, matching the practical backup use case.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    tz = ZoneInfo(timezone_name)
    now = _parse_now(now_iso, tz)
    today_midnight = datetime.combine(now.date(), time.min, tzinfo=tz)
    start = today_midnight - timedelta(days=days)
    end = today_midnight + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)
