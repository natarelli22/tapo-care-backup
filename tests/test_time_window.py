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


def test_resolve_timezone_from_supervisor_token(monkeypatch=None):
    from unittest.mock import patch, MagicMock
    import io

    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = io.BytesIO(b'{"time_zone": "America/Sao_Paulo"}')

    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "mock_token", "TZ": "UTC"}):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            tz = resolve_timezone()
            assert tz == ZoneInfo("America/Sao_Paulo")


def test_resolve_timezone_from_ha_storage(monkeypatch=None):
    from unittest.mock import patch, mock_open

    mock_json = '{"data": {"time_zone": "America/Sao_Paulo"}}'
    with patch("os.path.isfile", side_effect=lambda p: p == "/config/.storage/core.config"):
        with patch("builtins.open", mock_open(read_data=mock_json)):
            tz = resolve_timezone()
            assert tz == ZoneInfo("America/Sao_Paulo")


