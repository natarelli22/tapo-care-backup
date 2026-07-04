import pytest
import tapo_care_backup.monitor as monitor
from tapo_care_backup.monitor import (
    SavedClip,
    WatchPaths,
    WatchResult,
    WatchSettings,
    format_slack_message,
    load_env_file,
    load_state,
    prepare_attachment_path,
    prepare_grid_attachment_path,
    safe_output_path,
    save_state,
    settings_from_env,
    stable_clip_id,
)
from tapo_care_backup.video_index import DownloadCandidate, iter_download_candidates


def test_load_env_file_handles_quotes_and_comments(tmp_path):
    env_file = tmp_path / "monitor.env"
    env_file.write_text("""
# comment
TAPO_USERNAME='kan@example.com'
TAPO_PASSWORD=redacted
OTHER=value
BROKEN
""".strip())

    assert load_env_file(env_file) == {
        "TAPO_USERNAME": "kan@example.com",
        "TAPO_PASSWORD": "redacted",
        "OTHER": "value",
    }


def test_stable_clip_id_ignores_temporary_url_hash_suffix():
    a = DownloadCandidate("Front", "2026-06-20 12:34:56", "https://example.test/a?sig=1", None, "Front/2026-06-20/2026-06-20_12-34-56_0_aaaaaaaaaa.ts")
    b = DownloadCandidate("Front", "2026-06-20 12:34:56", "https://example.test/a?sig=2", None, "Front/2026-06-20/2026-06-20_12-34-56_0_bbbbbbbbbb.ts")

    assert stable_clip_id("device-1", a) == stable_clip_id("device-1", b)
    assert stable_clip_id("device-2", a) != stable_clip_id("device-1", b)


def test_candidate_paths_sanitize_api_derived_event_times():
    payload = {"index": [{"eventLocalTime": "../../escape 12:34:56", "video": [{"uri": "https://example.test/a.ts"}]}]}

    candidate = next(iter_download_candidates(payload, device_alias="../Front Door"))

    assert ".." not in candidate.relative_path
    assert candidate.relative_path.startswith("Front_Door/esca/escape_12-34-56_")


def test_safe_output_path_rejects_traversal(tmp_path):
    with pytest.raises(Exception):
        safe_output_path(tmp_path, "../outside.ts")

    assert safe_output_path(tmp_path, "cam/file.ts") == (tmp_path / "cam" / "file.ts").resolve()


def test_run_watch_once_honors_path_settings_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "monitor.env"
    output_dir = tmp_path / "custom-output"
    session_file = tmp_path / "custom-session.json"
    state_file = tmp_path / "custom-state.json"
    env_file.write_text(f"""
TAPO_WATCH_OUTPUT_DIR={output_dir}
TAPO_WATCH_SESSION={session_file}
TAPO_WATCH_STATE={state_file}
""".strip())
    captured = {}

    def fake_load_or_login_session(paths):
        captured["paths"] = paths
        return None

    monkeypatch.setenv("TAPO_WATCH_ENV_FILE", str(env_file))
    monkeypatch.delenv("TAPO_WATCH_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("TAPO_WATCH_SESSION", raising=False)
    monkeypatch.delenv("TAPO_WATCH_STATE", raising=False)
    monkeypatch.setattr(monitor, "load_or_login_session", fake_load_or_login_session)

    assert monitor.run_watch_once() is None
    assert captured["paths"].output_dir == output_dir
    assert captured["paths"].session_file == session_file
    assert captured["paths"].state_file == state_file


def test_settings_from_env_falls_back_to_mark_seen_for_unknown_bootstrap(monkeypatch):
    monkeypatch.setenv("TAPO_WATCH_BOOTSTRAP", "typo")

    assert settings_from_env().bootstrap_mode == "mark_seen"


def test_settings_from_env_defaults_to_mp4_attachments(monkeypatch):
    monkeypatch.delenv("TAPO_WATCH_ATTACHMENT_FORMAT", raising=False)
    assert settings_from_env().attachment_format == "mp4"

    monkeypatch.setenv("TAPO_WATCH_ATTACHMENT_FORMAT", "source")
    assert settings_from_env().attachment_format == "source"


def test_settings_from_env_parses_person_notification_filter(monkeypatch):
    monkeypatch.delenv("TAPO_WATCH_NOTIFY_EVENT_TYPES", raising=False)
    assert settings_from_env().notify_event_types is None

    monkeypatch.setenv("TAPO_WATCH_NOTIFY_EVENT_TYPES", "person, motion, PD")
    assert settings_from_env().notify_event_types == ("PD", "MOTION")

    monkeypatch.setenv("TAPO_WATCH_NOTIFY_EVENT_TYPES", "all")
    assert settings_from_env().notify_event_types is None


def test_settings_from_env_parses_grid_attachment_flag(monkeypatch):
    monkeypatch.delenv("TAPO_WATCH_GRID_ATTACHMENTS", raising=False)
    assert settings_from_env().grid_attachments is False

    monkeypatch.setenv("TAPO_WATCH_GRID_ATTACHMENTS", "1")
    assert settings_from_env().grid_attachments is True

    monkeypatch.setenv("TAPO_WATCH_GRID_TILE_WIDTH", "320")
    monkeypatch.setenv("TAPO_WATCH_GRID_TILE_HEIGHT", "180")
    settings = settings_from_env()
    assert settings.grid_tile_width == 320
    assert settings.grid_tile_height == 180


def test_settings_from_env_parses_notify_clips_per_run(monkeypatch):
    monkeypatch.delenv("TAPO_WATCH_NOTIFY_CLIPS_PER_RUN", raising=False)
    assert settings_from_env().notify_clips_per_run is None

    monkeypatch.setenv("TAPO_WATCH_NOTIFY_CLIPS_PER_RUN", "1")
    assert settings_from_env().notify_clips_per_run == 1

    monkeypatch.setenv("TAPO_WATCH_NOTIFY_CLIPS_PER_RUN", "0")
    assert settings_from_env().notify_clips_per_run is None


def test_settings_from_env_grid_mode_notifies_all_even_with_person_filter(monkeypatch):
    monkeypatch.setenv("TAPO_WATCH_GRID_ATTACHMENTS", "1")
    monkeypatch.setenv("TAPO_WATCH_NOTIFY_EVENT_TYPES", "person")

    settings = settings_from_env()

    assert settings.grid_attachments is True
    assert settings.notify_event_types is None


def test_prepare_attachment_path_remuxes_ts_to_mp4(tmp_path, monkeypatch):
    source = tmp_path / "clip.ts"
    source.write_bytes(b"ts")
    calls = []

    monkeypatch.setattr(monitor.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def fake_run(cmd, check, timeout, stdin, stdout, stderr):
        calls.append((cmd, check, timeout, stdin, stdout, stderr))
        tmp = tmp_path / "clip.tmp.mp4"
        tmp.write_bytes(b"mp4")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)

    result = prepare_attachment_path(source)

    assert result == tmp_path / "clip.mp4"
    assert result.read_bytes() == b"mp4"
    assert calls and calls[0][0][0] == "/usr/bin/ffmpeg"
    assert "-f" in calls[0][0] and calls[0][0][-2:] == ["mp4", str(tmp_path / "clip.tmp.mp4")]


def test_prepare_attachment_path_falls_back_without_ffmpeg(tmp_path, monkeypatch):
    source = tmp_path / "clip.ts"
    source.write_bytes(b"ts")
    monkeypatch.setattr(monitor.shutil, "which", lambda name: None)

    assert prepare_attachment_path(source) == source


def test_prepare_grid_attachment_path_stacks_multiple_clips(tmp_path, monkeypatch):
    one = tmp_path / "one.mp4"
    two = tmp_path / "two.mp4"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    clips = [
        SavedClip("front", "2026-06-20 12:00:00", one, "clip-1", ("PD",), True),
        SavedClip("side", "2026-06-20 12:01:00", two, "clip-2", ("PD",), True),
    ]
    calls = []

    monkeypatch.setattr(monitor.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def fake_run(cmd, check, timeout, stdin, stdout, stderr):
        calls.append((cmd, check, timeout, stdin, stdout, stderr))
        tmp_output = tmp_path / (cmd[-1].split("/")[-1])
        tmp_output.write_bytes(b"grid")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)

    result = prepare_grid_attachment_path(clips, tile_width=320, tile_height=180)

    assert result is not None
    assert result.name.startswith("tapo_grid_2_")
    assert result.suffix == ".mp4"
    assert result.read_bytes() == b"grid"
    cmd = calls[0][0]
    assert cmd[:2] == ["/usr/bin/ffmpeg", "-nostdin"]
    assert cmd.count("-i") == 2
    assert "xstack=inputs=2:layout=0_0|320_0:fill=black:shortest=0[vout]" in cmd[cmd.index("-filter_complex") + 1]
    assert "-an" in cmd
    assert "libx264" in cmd


def test_prepare_grid_attachment_path_falls_back_for_single_clip(tmp_path):
    clip = SavedClip("front", "2026-06-20 12:00:00", tmp_path / "one.mp4", "clip-1", ("PD",), True)

    assert prepare_grid_attachment_path([clip]) is None


def test_list_camera_devices_includes_tapo_doorbells(monkeypatch, tmp_path):
    devices = [
        monitor.TapoDevice("camera-1", "camera", "SMART.IPCAMERA", "C210"),
        monitor.TapoDevice("doorbell-1", "intercom", "SMART.TAPODOORBELL", "D205"),
        monitor.TapoDevice("plug-1", "plug", "SMART.TAPOPLUG", "P110M"),
    ]

    class FakeCloud:
        def list_devices(self, session):
            return devices

    monkeypatch.setattr(monitor, "TapoCloudClient", lambda: FakeCloud())
    paths = WatchPaths(tmp_path / "env", tmp_path / "session", tmp_path / "state", tmp_path / "out")

    result = monitor.list_camera_devices(monitor.StoredSession("token", "user@example.test", "aps1"), paths)

    assert [device.alias for device in result] == ["camera", "intercom"]


def test_state_file_is_user_only(tmp_path):
    state_path = tmp_path / "watch_state.json"
    save_state(state_path, {"version": 1, "bootstrapped": True, "seen": {"x": {}}})

    assert load_state(state_path)["bootstrapped"] is True
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"


def test_format_slack_message_is_quiet_without_new_clips(tmp_path):
    assert format_slack_message(WatchResult(False, 3, [])) == ""
    assert format_slack_message(WatchResult(True, 3, [])) == ""


def test_format_slack_message_can_notify_bootstrap_when_requested():
    message = format_slack_message(WatchResult(True, 3, []), notify_bootstrap=True)

    assert "既存3件" in message


def test_format_slack_message_includes_limited_media_paths(tmp_path):
    clips = [
        (tmp_path / "one.ts"),
        (tmp_path / "two.ts"),
        (tmp_path / "three.ts"),
    ]
    result = WatchResult(
        bootstrapped=False,
        checked_candidates=3,
        saved=[
            SavedClip("cam", "2026-06-20 12:00:00", clips[0], "1"),
            SavedClip("cam", "2026-06-20 12:05:00", clips[1], "2"),
            SavedClip("cam", "2026-06-20 12:10:00", clips[2], "3"),
        ],
    )

    message = format_slack_message(result, max_attachments=2)

    assert "新規録画: 3件" in message
    assert f"MEDIA:{clips[0]}" in message
    assert f"MEDIA:{clips[1]}" in message
    assert f"MEDIA:{clips[2]}" not in message
    assert "ほか1件" in message


def test_format_slack_message_filters_non_notifiable_saved_clips(tmp_path):
    motion = tmp_path / "motion.mp4"
    person = tmp_path / "person.mp4"
    result = WatchResult(
        bootstrapped=False,
        checked_candidates=2,
        saved=[
            SavedClip("cam", "2026-06-20 12:00:00", motion, "motion", ("MOTION",), False),
            SavedClip("cam", "2026-06-20 12:01:00", person, "person", ("PD",), True),
        ],
        notification_filter=("PD",),
    )

    message = format_slack_message(result, max_attachments=20)

    assert "人物検知録画: 1件" in message
    assert person.name in message
    assert f"MEDIA:{person}" in message
    assert motion.name not in message
    assert f"MEDIA:{motion}" not in message


def test_format_slack_message_uses_one_grid_media_attachment_when_available(tmp_path):
    one = tmp_path / "one.mp4"
    two = tmp_path / "two.mp4"
    grid = tmp_path / "grid.mp4"
    result = WatchResult(
        bootstrapped=False,
        checked_candidates=2,
        saved=[
            SavedClip("front", "2026-06-20 12:00:00", one, "1", ("PD",), True),
            SavedClip("side", "2026-06-20 12:01:00", two, "2", ("PD",), True),
        ],
        notification_filter=("PD",),
        combined_attachment=grid,
    )

    message = format_slack_message(result, max_attachments=20)

    assert message.count("MEDIA:") == 1
    assert f"MEDIA:{grid}" in message
    assert "one.mp4" in message
    assert "two.mp4" in message
    assert f"MEDIA:{one}" not in message
    assert f"MEDIA:{two}" not in message


def test_run_watch_once_saves_all_new_clips_but_only_notifies_person(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    settings = WatchSettings(
        bootstrap_mode="download_existing",
        attachment_format="source",
        notify_event_types=("PD",),
        max_attachments=20,
    )
    motion = DownloadCandidate(
        "cam",
        "2026-06-20 12:00:00",
        "https://example.test/motion.ts",
        None,
        "cam/2026-06-20/motion.ts",
        ("MOTION",),
    )
    person = DownloadCandidate(
        "cam",
        "2026-06-20 12:01:00",
        "https://example.test/person.ts",
        None,
        "cam/2026-06-20/person.ts",
        ("PD",),
    )

    class FakeCare:
        def __init__(self, session):
            pass

        def download_bytes(self, url):
            return url.encode()

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: [monitor.TapoDevice("device-1", "cam", "SMART.IPCAMERA")])
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: [("device-1", motion), ("device-1", person)])
    monkeypatch.setattr(monitor, "TapoCareClient", FakeCare)

    result = monitor.run_watch_once(paths, settings)

    assert result is not None
    assert [clip.path.name for clip in result.saved] == ["motion.ts", "person.ts"]
    assert [clip.notify for clip in result.saved] == [False, True]
    assert (paths.output_dir / motion.relative_path).exists()
    assert (paths.output_dir / person.relative_path).exists()
    state = load_state(paths.state_file)
    assert len(state["seen"]) == 2
    assert {tuple(item["event_types"]) for item in state["seen"].values()} == {("MOTION",), ("PD",)}

    message = format_slack_message(result, max_attachments=20)
    assert "person.ts" in message
    assert "motion.ts" not in message


def test_run_watch_once_downloads_all_new_clips_but_drains_notifications_one_per_run(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    settings = WatchSettings(
        bootstrap_mode="download_existing",
        attachment_format="source",
        max_attachments=1,
        grid_attachments=False,
        notify_clips_per_run=1,
    )
    candidates = [
        DownloadCandidate("cam", "2026-06-20 12:03:00", "https://example.test/3.ts", None, "cam/2026-06-20/3.ts", ("MOTION",)),
        DownloadCandidate("cam", "2026-06-20 12:02:00", "https://example.test/2.ts", None, "cam/2026-06-20/2.ts", ("MOTION",)),
        DownloadCandidate("cam", "2026-06-20 12:01:00", "https://example.test/1.ts", None, "cam/2026-06-20/1.ts", ("MOTION",)),
    ]
    candidate_batches = [[("device-1", candidate) for candidate in candidates], [], [], []]

    class FakeCare:
        def __init__(self, session):
            pass

        def download_bytes(self, url):
            return url.encode()

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: [monitor.TapoDevice("device-1", "cam", "SMART.IPCAMERA")])
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: candidate_batches.pop(0))
    monkeypatch.setattr(monitor, "TapoCareClient", FakeCare)

    first_result = monitor.run_watch_once(paths, settings)

    assert first_result is not None
    assert [clip.path.name for clip in first_result.saved] == ["3.ts"]
    state = load_state(paths.state_file)
    assert len(state["seen"]) == 3
    assert [item["path"] for item in state["pending_notifications"]] == [
        str(paths.output_dir / "cam/2026-06-20/2.ts"),
        str(paths.output_dir / "cam/2026-06-20/1.ts"),
    ]
    assert all((paths.output_dir / candidate.relative_path).exists() for candidate in candidates)
    first_message = format_slack_message(first_result, max_attachments=1)
    assert first_message.count("MEDIA:") == 1
    assert "3.ts" in first_message
    assert "2.ts" not in first_message

    second_result = monitor.run_watch_once(paths, settings)

    assert second_result is not None
    assert [clip.path.name for clip in second_result.saved] == ["2.ts"]
    state = load_state(paths.state_file)
    assert len(state["seen"]) == 3
    assert [item["path"] for item in state["pending_notifications"]] == [str(paths.output_dir / "cam/2026-06-20/1.ts")]
    second_message = format_slack_message(second_result, max_attachments=1)
    assert second_message.count("MEDIA:") == 1
    assert "2.ts" in second_message
    assert "1.ts" not in second_message

    third_result = monitor.run_watch_once(paths, settings)
    assert third_result is not None
    assert [clip.path.name for clip in third_result.saved] == ["1.ts"]
    assert load_state(paths.state_file)["pending_notifications"] == []

    fourth_result = monitor.run_watch_once(paths, settings)
    assert fourth_result is not None
    assert fourth_result.saved == []
    assert format_slack_message(fourth_result, max_attachments=1) == ""


def test_run_watch_once_drains_existing_pending_before_tapo_api_calls(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"mp4")
    save_state(
        paths.state_file,
        {
            "version": 1,
            "bootstrapped": True,
            "seen": {},
            "pending_notifications": [
                {
                    "device_alias": "cam",
                    "event_local_time": "2026-06-20 12:01:00",
                    "path": str(existing),
                    "clip_id": "existing",
                    "event_types": ["MOTION"],
                }
            ],
        },
    )
    settings = WatchSettings(bootstrap_mode="download_existing", attachment_format="source", max_attachments=1, notify_clips_per_run=1)

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: pytest.fail("pending queue should drain before Tapo API calls"))
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: pytest.fail("pending queue should drain before Tapo API calls"))

    result = monitor.run_watch_once(paths, settings)

    assert result is not None
    assert [clip.path for clip in result.saved] == [existing]
    assert load_state(paths.state_file)["pending_notifications"] == []


def test_run_watch_once_skips_missing_pending_notification_files(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"mp4")
    save_state(
        paths.state_file,
        {
            "version": 1,
            "bootstrapped": True,
            "seen": {},
            "pending_notifications": [
                {
                    "device_alias": "cam",
                    "event_local_time": "2026-06-20 12:00:00",
                    "path": str(tmp_path / "missing.mp4"),
                    "clip_id": "missing",
                    "event_types": ["MOTION"],
                },
                {
                    "device_alias": "cam",
                    "event_local_time": "2026-06-20 12:01:00",
                    "path": str(existing),
                    "clip_id": "existing",
                    "event_types": ["MOTION"],
                },
            ],
        },
    )
    settings = WatchSettings(bootstrap_mode="download_existing", attachment_format="source", max_attachments=1, notify_clips_per_run=1)

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: [])
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: [])

    result = monitor.run_watch_once(paths, settings)

    assert result is not None
    assert [clip.path for clip in result.saved] == [existing]
    assert load_state(paths.state_file)["pending_notifications"] == []


def test_run_watch_once_combines_multiple_notified_clips_into_grid(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    settings = WatchSettings(
        bootstrap_mode="download_existing",
        attachment_format="source",
        notify_event_types=("PD",),
        max_attachments=20,
        grid_attachments=True,
    )
    first = DownloadCandidate("cam", "2026-06-20 12:01:00", "https://example.test/first.ts", None, "cam/2026-06-20/first.ts", ("MOTION",))
    second = DownloadCandidate("cam", "2026-06-20 12:02:00", "https://example.test/second.ts", None, "cam/2026-06-20/second.ts", ("PD",))
    grid = tmp_path / "grid.mp4"
    captured = {}

    class FakeCare:
        def __init__(self, session):
            pass

        def download_bytes(self, url):
            return url.encode()

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: [monitor.TapoDevice("device-1", "cam", "SMART.IPCAMERA")])
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: [("device-1", first), ("device-1", second)])
    monkeypatch.setattr(monitor, "TapoCareClient", FakeCare)

    def fake_grid(clips, tile_width, tile_height):
        captured["grid_clips"] = clips
        return grid

    monkeypatch.setattr(monitor, "prepare_grid_attachment_path", fake_grid)

    result = monitor.run_watch_once(paths, settings)

    assert result is not None
    assert result.notification_filter is None
    assert [clip.notify for clip in result.saved] == [True, True]
    assert [clip.path.name for clip in captured["grid_clips"]] == ["first.ts", "second.ts"]
    assert result.combined_attachment == grid
    message = format_slack_message(result, max_attachments=20)
    assert "Tapo Care新規録画: 2件" in message
    assert "モーション" in message
    assert message.count("MEDIA:") == 1
    assert f"MEDIA:{grid}" in message
    assert "first.ts" in message
    assert "second.ts" in message


def test_grid_attachment_includes_all_notified_clips_even_when_message_list_is_limited(tmp_path, monkeypatch):
    paths = WatchPaths(
        env_file=tmp_path / "missing.env",
        session_file=tmp_path / "session.json",
        state_file=tmp_path / "state.json",
        output_dir=tmp_path / "out",
    )
    settings = WatchSettings(
        bootstrap_mode="download_existing",
        attachment_format="source",
        max_attachments=12,
        grid_attachments=True,
    )
    candidates = [
        DownloadCandidate(
            "cam",
            f"2026-06-20 12:{index:02d}:00",
            f"https://example.test/{index}.ts",
            None,
            f"cam/2026-06-20/{index}.ts",
            ("MOTION",),
        )
        for index in range(27)
    ]
    grid = tmp_path / "grid.mp4"
    captured = {}

    class FakeCare:
        def __init__(self, session):
            pass

        def download_bytes(self, url):
            return url.encode()

    monkeypatch.setattr(monitor, "load_or_login_session", lambda paths: object())
    monkeypatch.setattr(monitor, "list_camera_devices", lambda session, paths: [monitor.TapoDevice("device-1", "cam", "SMART.IPCAMERA")])
    monkeypatch.setattr(monitor, "iter_candidates_for_devices", lambda session, devices, settings: [("device-1", candidate) for candidate in candidates])
    monkeypatch.setattr(monitor, "TapoCareClient", FakeCare)

    def fake_grid(clips, tile_width, tile_height):
        captured["grid_clips"] = clips
        return grid

    monkeypatch.setattr(monitor, "prepare_grid_attachment_path", fake_grid)

    result = monitor.run_watch_once(paths, settings)

    assert result is not None
    assert len(result.saved) == 27
    assert len(captured["grid_clips"]) == 27
    assert [clip.path.name for clip in captured["grid_clips"]] == [f"{index}.ts" for index in range(27)]
    message = format_slack_message(result, max_attachments=12)
    assert message.count("MEDIA:") == 1
    assert f"MEDIA:{grid}" in message
    assert "ほか15件もグリッド内に含めて保存済みです。" in message
    assert "12.ts" not in message
