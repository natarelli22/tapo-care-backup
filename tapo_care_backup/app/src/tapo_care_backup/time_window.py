"""Time-window helpers for Tapo Care list requests."""
from __future__ import annotations

from datetime import datetime, timedelta, time
import os
from zoneinfo import ZoneInfo


def resolve_timezone(timezone_name: str | None = None) -> ZoneInfo:
    """Resolve a valid ZoneInfo instance.

    If `timezone_name` is explicitly provided, it is parsed directly.
    Otherwise, auto-discovers the timezone from:
      1. $TZ environment variable (set by Home Assistant Supervisor)
      2. /etc/timezone
      3. /etc/localtime symlink target
      4. System local timezone (datetime.now().astimezone())
      5. Fallback: "UTC"
    """
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass

    # 1. Direct read from Home Assistant Core storage (/config/.storage/core.config)
    if os.path.isfile("/config/.storage/core.config"):
        try:
            import json

            with open("/config/.storage/core.config", "r", encoding="utf-8") as fp:
                data = json.load(fp)
                ha_tz = data.get("data", {}).get("time_zone")
                if ha_tz:
                    return ZoneInfo(str(ha_tz))
        except Exception:
            pass

    # 2. Query Home Assistant Core / Supervisor API if running inside an add-on
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        for url in ("http://supervisor/core/api/config", "http://supervisor/info"):
            try:
                import json
                import urllib.request

                req = urllib.request.Request(
                    url,
                    headers={"Authorization": f"Bearer {supervisor_token}"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    payload = json.loads(resp.read().decode())
                    candidate = (
                        payload.get("time_zone")
                        or payload.get("timezone")
                        or payload.get("data", {}).get("timezone")
                    )
                    if candidate:
                        return ZoneInfo(str(candidate))
            except Exception:
                pass

    # 2. $TZ environment variable
    tz_env = os.environ.get("TZ")
    if tz_env and tz_env.strip() != "UTC":
        try:
            return ZoneInfo(tz_env.strip())
        except Exception:
            pass

    # 3. /etc/timezone
    if os.path.isfile("/etc/timezone"):
        try:
            with open("/etc/timezone", "r", encoding="utf-8") as fp:
                val = fp.read().strip()
                if val and val != "UTC":
                    return ZoneInfo(val)
        except Exception:
            pass

    # 4. /etc/localtime symlink
    if os.path.islink("/etc/localtime"):
        try:
            real_path = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in real_path:
                val = real_path.split("zoneinfo/")[-1]
                return ZoneInfo(val)
        except Exception:
            pass

    # 5. Local system timezone via datetime
    try:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None:
            key = getattr(local_tz, "key", None)
            if key:
                return ZoneInfo(key)
            return local_tz  # type: ignore[return-value]
    except Exception:
        pass

    if tz_env:
        try:
            return ZoneInfo(tz_env.strip())
        except Exception:
            pass

    return ZoneInfo("UTC")


def _parse_now(now_iso: str | None, tz: ZoneInfo) -> datetime:
    if now_iso:
        parsed = datetime.fromisoformat(now_iso)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    return datetime.now(tz)


def build_time_window(days: int, timezone_name: str | None = None, now_iso: str | None = None) -> tuple[str, str]:
    """Build `[start, end]` strings using local-day boundaries.

    The end is tomorrow's midnight so today's partial clips are included; `days=1`
    asks for yesterday + today, matching the practical backup use case.
    If `timezone_name` is None, automatically resolves the Home Assistant/system timezone.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    tz = resolve_timezone(timezone_name)
    now = _parse_now(now_iso, tz)
    today_midnight = datetime.combine(now.date(), time.min, tzinfo=tz)
    start = today_midnight - timedelta(days=days)
    end = today_midnight + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)
