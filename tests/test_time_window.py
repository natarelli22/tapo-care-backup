import os
from zoneinfo import ZoneInfo
from tapo_care_backup.time_window import build_time_window, resolve_timezone


def test_build_time_window_uses_local_midnight_in_requested_timezone():
    start, end = build_time_window(days=1, timezone_name="Asia/Tokyo", now_iso="2026-06-20T15:30:00+09:00")

    assert start == "2026-06-19 00:00:00"
    assert end == "2026-06-21 00:00:00"


def test_resolve_timezone_explicit():
    tz = resolve_timezone("Europe/London")
    assert tz == ZoneInfo("Europe/London")


def test_resolve_timezone_from_env_tz():
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Sao_Paulo"
        tz = resolve_timezone()
        assert tz == ZoneInfo("America/Sao_Paulo")
    finally:
        if old_tz is not None:
            os.environ["TZ"] = old_tz
        else:
            os.environ.pop("TZ", None)


def test_build_time_window_auto_detect_from_env():
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Sao_Paulo"
        start, end = build_time_window(days=1, now_iso="2026-09-20T16:00:00-03:00")
        assert start == "2026-09-19 00:00:00"
        assert end == "2026-09-21 00:00:00"
    finally:
        if old_tz is not None:
            os.environ["TZ"] = old_tz
        else:
            os.environ.pop("TZ", None)
