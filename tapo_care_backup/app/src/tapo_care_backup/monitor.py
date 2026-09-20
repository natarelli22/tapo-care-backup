"""Incremental Tapo Care monitoring helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable, Mapping, Sequence

from .api import TapoApiError, TapoCareClient, TapoCloudClient, TapoDevice
from .config import StoredSession, load_session, save_session
from .crypto import decrypt_tapo_payload
from .time_window import build_time_window
from .video_index import DownloadCandidate, iter_download_candidates

_URL_HASH_SUFFIX = re.compile(r"_[0-9a-f]{10}\.ts$")
_EVENT_TYPE_ALIASES = {
    "PERSON": "PD",
    "PERSON_DETECTION": "PD",
    "HUMAN": "PD",
    "HUMAN_DETECTION": "PD",
    "PEOPLE": "PD",
    "人物": "PD",
    "人": "PD",
    "MOTION": "MOTION",
    "MOTION_DETECTION": "MOTION",
    "動体": "MOTION",
}
_EVENT_TYPE_LABELS = {"PD": "人物検知", "MOTION": "モーション"}
_PENDING_NOTIFICATIONS_KEY = "pending_notifications"


@dataclass(frozen=True)
class WatchPaths:
    env_file: Path
    session_file: Path
    state_file: Path
    output_dir: Path


@dataclass(frozen=True)
class WatchSettings:
    days: int = 1
    timezone_name: str = "Asia/Tokyo"
    page_size: int = 500
    max_attachments: int = 3
    bootstrap_mode: str = "mark_seen"
    attachment_format: str = "mp4"
    notify_event_types: tuple[str, ...] | None = None
    grid_attachments: bool = False
    grid_tile_width: int = 480
    grid_tile_height: int = 270
    notify_clips_per_run: int | None = None
    notify_queue_policy: str = "fifo"
    device_id: str | None = None


@dataclass(frozen=True)
class SavedClip:
    device_alias: str
    event_local_time: str
    path: Path
    clip_id: str
    event_types: tuple[str, ...] = ()
    notify: bool = True


@dataclass(frozen=True)
class WatchResult:
    bootstrapped: bool
    checked_candidates: int
    saved: list[SavedClip]
    notification_filter: tuple[str, ...] | None = None
    combined_attachment: Path | None = None


def default_watch_paths() -> WatchPaths:
    config_dir = Path(os.environ.get("TAPO_CARE_BACKUP_CONFIG_DIR", Path.home() / ".config" / "tapo-care-backup"))
    state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tapo-care-backup"
    return WatchPaths(
        env_file=Path(os.environ.get("TAPO_WATCH_ENV_FILE", config_dir / "monitor.env")).expanduser(),
        session_file=Path(os.environ.get("TAPO_WATCH_SESSION", config_dir / "session.json")).expanduser(),
        state_file=Path(os.environ.get("TAPO_WATCH_STATE", state_dir / "watch_state.json")).expanduser(),
        output_dir=Path(os.environ.get("TAPO_WATCH_OUTPUT_DIR", Path.home() / "TapoBackups")).expanduser(),
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Load a small KEY=VALUE env file without requiring python-dotenv."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def apply_env_file(values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if key.startswith(("TAPO_", "TAPO_WATCH_")):
            os.environ.setdefault(key, value)


def normalize_event_type(value: str) -> str:
    raw = value.strip()
    folded = raw.upper().replace("-", "_").replace(" ", "_")
    return _EVENT_TYPE_ALIASES.get(folded) or _EVENT_TYPE_ALIASES.get(raw) or folded


def parse_notify_event_types(value: str | None) -> tuple[str, ...] | None:
    if value is None or not value.strip():
        return None
    if value.strip().lower() in {"*", "all", "any"}:
        return None
    event_types: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\s]+", value):
        if not part:
            continue
        event_type = normalize_event_type(part)
        if event_type and event_type not in seen:
            seen.add(event_type)
            event_types.append(event_type)
    return tuple(event_types) or None


def should_notify_candidate(candidate: DownloadCandidate, notify_event_types: tuple[str, ...] | None) -> bool:
    if notify_event_types is None:
        return True
    candidate_event_types = {normalize_event_type(event_type) for event_type in candidate.event_types}
    return bool(candidate_event_types.intersection(notify_event_types))


def _env_truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on", "grid", "auto"})


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_optional_positive_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_notify_queue_policy() -> str:
    value = os.environ.get("TAPO_WATCH_NOTIFY_QUEUE_POLICY", "fifo").strip().lower()
    if value in {"fifo", "latest"}:
        return value
    return "fifo"


def effective_notify_event_types(settings: WatchSettings) -> tuple[str, ...] | None:
    """Return outbound notification filter after mode-level overrides.

    Grid mode is intended as a compact review surface, so it sends all event
    categories even if a legacy person-only filter remains in the local env file.
    """
    if settings.grid_attachments:
        return None
    return settings.notify_event_types


def settings_from_env() -> WatchSettings:
    bootstrap_mode = os.environ.get("TAPO_WATCH_BOOTSTRAP", "mark_seen")
    if bootstrap_mode not in {"mark_seen", "download_existing"}:
        bootstrap_mode = "mark_seen"
    attachment_format = os.environ.get("TAPO_WATCH_ATTACHMENT_FORMAT", "mp4").strip().lower()
    if attachment_format not in {"mp4", "source"}:
        attachment_format = "mp4"
    grid_attachments = _env_truthy(os.environ.get("TAPO_WATCH_GRID_ATTACHMENTS"))
    notify_event_types = None if grid_attachments else parse_notify_event_types(os.environ.get("TAPO_WATCH_NOTIFY_EVENT_TYPES"))
    return WatchSettings(
        days=int(os.environ.get("TAPO_WATCH_DAYS", "1")),
        timezone_name=os.environ.get("TAPO_WATCH_TIMEZONE", "Asia/Tokyo"),
        page_size=int(os.environ.get("TAPO_WATCH_PAGE_SIZE", "500")),
        max_attachments=int(os.environ.get("TAPO_WATCH_MAX_ATTACHMENTS", "3")),
        bootstrap_mode=bootstrap_mode,
        attachment_format=attachment_format,
        notify_event_types=notify_event_types,
        grid_attachments=grid_attachments,
        grid_tile_width=_env_positive_int("TAPO_WATCH_GRID_TILE_WIDTH", 480),
        grid_tile_height=_env_positive_int("TAPO_WATCH_GRID_TILE_HEIGHT", 270),
        notify_clips_per_run=_env_optional_positive_int("TAPO_WATCH_NOTIFY_CLIPS_PER_RUN"),
        notify_queue_policy=_env_notify_queue_policy(),
        device_id=os.environ.get("TAPO_WATCH_DEVICE_ID") or None,
    )


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "bootstrapped": False, "seen": {}, _PENDING_NOTIFICATIONS_KEY: []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", 1)
    data.setdefault("bootstrapped", False)
    data.setdefault("seen", {})
    data.setdefault(_PENDING_NOTIFICATIONS_KEY, [])
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def stable_clip_id(device_id: str, candidate: DownloadCandidate) -> str:
    """Return a non-secret stable identifier for a clip.

    Tapo media URLs can be signed/temporary, so only use the generated path after
    removing its URL-hash suffix. The raw device ID and URL are never persisted.
    """
    stable_path = _URL_HASH_SUFFIX.sub(".ts", candidate.relative_path)
    material = "|".join([device_id, candidate.device_alias, candidate.event_local_time, stable_path])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _credentials_available() -> bool:
    return bool(os.environ.get("TAPO_USERNAME") and os.environ.get("TAPO_PASSWORD"))


def load_or_login_session(paths: WatchPaths) -> StoredSession | None:
    if paths.session_file.exists():
        return load_session(paths.session_file)
    if not _credentials_available():
        return None
    session = TapoCloudClient().login_legacy(os.environ["TAPO_USERNAME"], os.environ["TAPO_PASSWORD"])
    save_session(session, paths.session_file)
    return session


def _login_refresh(paths: WatchPaths) -> StoredSession:
    if not _credentials_available():
        raise TapoApiError("No saved session and TAPO_USERNAME/TAPO_PASSWORD are not configured")
    session = TapoCloudClient().login_legacy(os.environ["TAPO_USERNAME"], os.environ["TAPO_PASSWORD"])
    save_session(session, paths.session_file)
    return session


def is_recording_device(device: TapoDevice) -> bool:
    device_type = device.device_type.upper()
    return device.device_type == "SMART.IPCAMERA" or "CAMERA" in device_type or "DOORBELL" in device_type


def list_camera_devices(session: StoredSession, paths: WatchPaths) -> list[TapoDevice]:
    cloud = TapoCloudClient()
    try:
        devices = cloud.list_devices(session)
    except TapoApiError:
        session = _login_refresh(paths)
        devices = cloud.list_devices(session)
    return [d for d in devices if is_recording_device(d)]


def iter_candidates_for_devices(session: StoredSession, devices: Iterable[TapoDevice], settings: WatchSettings):
    care = TapoCareClient(session)
    start, end = build_time_window(settings.days, settings.timezone_name)
    for device in devices:
        alias = device.alias or device.device_id
        for payload in care.iter_video_pages(device.device_id, start, end, page_size=settings.page_size):
            for candidate in iter_download_candidates(payload, alias):
                yield device.device_id, candidate


def safe_output_path(output_dir: Path, relative_path: str) -> Path:
    """Resolve a candidate path and ensure it stays inside output_dir."""
    base = output_dir.resolve()
    path = (base / relative_path).resolve()
    if not path.is_relative_to(base):
        raise TapoApiError("Unsafe Tapo Care output path rejected")
    return path


def prepare_attachment_path(source_path: Path, attachment_format: str = "mp4") -> Path:
    """Return a Slack/Hermes-friendly attachment path for a saved clip.

    Tapo Care downloads are MPEG-TS (``.ts``). Hermes/Slack media extraction
    only treats common video/document extensions as native attachments, so a raw
    ``MEDIA:/path/file.ts`` can render as literal text. When ffmpeg is available,
    remux the clip to sibling ``.mp4`` without re-encoding and use that path for
    outbound notifications while keeping the original ``.ts`` backup intact.
    """
    if attachment_format != "mp4" or source_path.suffix.lower() != ".ts":
        return source_path
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return source_path

    mp4_path = source_path.with_suffix(".mp4")
    try:
        if mp4_path.exists() and mp4_path.stat().st_mtime >= source_path.stat().st_mtime:
            return mp4_path
    except OSError:
        return source_path

    tmp_path = mp4_path.with_suffix(".tmp.mp4")
    try:
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(tmp_path),
            ],
            check=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        tmp_path.replace(mp4_path)
        return mp4_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return source_path


def _grid_output_path(clips: Sequence[SavedClip]) -> Path:
    material = "|".join([clip.clip_id for clip in clips] + [str(clip.path) for clip in clips])
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
    return clips[0].path.parent / f"tapo_grid_{len(clips)}_{digest}.mp4"


def _grid_filter(count: int, tile_width: int, tile_height: int) -> str:
    columns = math.ceil(math.sqrt(count))
    filters = [
        f"[{index}:v]scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
        f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=15[v{index}]"
        for index in range(count)
    ]
    layout = "|".join(f"{(index % columns) * tile_width}_{(index // columns) * tile_height}" for index in range(count))
    stack_inputs = "".join(f"[v{index}]" for index in range(count))
    filters.append(f"{stack_inputs}xstack=inputs={count}:layout={layout}:fill=black:shortest=0[vout]")
    return ";".join(filters)


def _pending_item_from_clip(clip: SavedClip) -> dict[str, object]:
    return {
        "device_alias": clip.device_alias,
        "event_local_time": clip.event_local_time,
        "path": str(clip.path),
        "clip_id": clip.clip_id,
        "event_types": list(clip.event_types),
    }


def _clip_from_pending_item(item: Mapping[str, object]) -> SavedClip | None:
    path_value = item.get("path")
    if not isinstance(path_value, str):
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    device_alias = item.get("device_alias")
    event_local_time = item.get("event_local_time")
    clip_id = item.get("clip_id")
    event_types = item.get("event_types")
    return SavedClip(
        device_alias if isinstance(device_alias, str) else "camera",
        event_local_time if isinstance(event_local_time, str) else "",
        path,
        clip_id if isinstance(clip_id, str) else hashlib.sha256(path_value.encode("utf-8")).hexdigest(),
        tuple(str(event_type) for event_type in event_types) if isinstance(event_types, list) else (),
        True,
    )


def _enqueue_pending_notifications(state: dict, clips: Sequence[SavedClip]) -> None:
    queue = state.setdefault(_PENDING_NOTIFICATIONS_KEY, [])
    if not isinstance(queue, list):
        queue = []
        state[_PENDING_NOTIFICATIONS_KEY] = queue
    queued_ids = {item.get("clip_id") for item in queue if isinstance(item, dict)}
    for clip in clips:
        if not clip.notify or clip.clip_id in queued_ids:
            continue
        queue.append(_pending_item_from_clip(clip))
        queued_ids.add(clip.clip_id)


def _drain_pending_notifications(state: dict, limit: int, queue_policy: str = "fifo") -> list[SavedClip]:
    queue = state.setdefault(_PENDING_NOTIFICATIONS_KEY, [])
    if not isinstance(queue, list):
        state[_PENDING_NOTIFICATIONS_KEY] = []
        return []
    if queue_policy == "latest":
        clips: list[SavedClip] = []
        for item in queue:
            if not isinstance(item, dict):
                continue
            clip = _clip_from_pending_item(item)
            if clip is None:
                continue
            clips.append(clip)
        clips.sort(key=lambda clip: clip.event_local_time, reverse=True)
        state[_PENDING_NOTIFICATIONS_KEY] = []
        return clips[:limit]
    drained: list[SavedClip] = []
    remaining: list[object] = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        clip = _clip_from_pending_item(item)
        if clip is None:
            continue
        if len(drained) < limit:
            drained.append(clip)
        else:
            remaining.append(item)
    state[_PENDING_NOTIFICATIONS_KEY] = remaining
    return drained


def prepare_grid_attachment_path(clips: Sequence[SavedClip], tile_width: int = 480, tile_height: int = 270) -> Path | None:
    """Create a single Slack-friendly MP4 grid for multiple notification clips.

    The original per-camera backups remain untouched. The grid is a derived
    notification artifact: video-only, H.264/yuv420p, and includes every
    notification clip passed by the caller. ``TAPO_WATCH_MAX_ATTACHMENTS`` only
    limits the Slack text list and individual-attachment fallback.
    """
    if len(clips) < 2:
        return None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    for clip in clips:
        if not clip.path.exists():
            return None

    grid_path = _grid_output_path(clips)
    try:
        newest_input = max(clip.path.stat().st_mtime for clip in clips)
        if grid_path.exists() and grid_path.stat().st_mtime >= newest_input:
            return grid_path
    except OSError:
        return None

    tmp_path = grid_path.with_suffix(".tmp.mp4")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for clip in clips:
        cmd.extend(["-i", str(clip.path)])
    cmd.extend(
        [
            "-filter_complex",
            _grid_filter(len(clips), tile_width, tile_height),
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(tmp_path),
        ]
    )
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp_path.replace(grid_path)
        return grid_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def run_watch_once(paths: WatchPaths | None = None, settings: WatchSettings | None = None) -> WatchResult | None:
    if paths is None:
        initial_paths = default_watch_paths()
        apply_env_file(load_env_file(initial_paths.env_file))
        paths = default_watch_paths()
    else:
        apply_env_file(load_env_file(paths.env_file))
    settings = settings or settings_from_env()
    notification_filter = effective_notify_event_types(settings)

    state = load_state(paths.state_file)

    session = load_or_login_session(paths)
    if session is None:
        # Cron-friendly: stay silent until credentials or a session token is configured.
        return None

    devices = list_camera_devices(session, paths)
    if settings.device_id:
        devices = [d for d in devices if d.device_id == settings.device_id]

    try:
        candidates = list(iter_candidates_for_devices(session, devices, settings))
    except TapoApiError:
        session = _login_refresh(paths)
        candidates = list(iter_candidates_for_devices(session, devices, settings))
    seen: dict[str, dict] = state.setdefault("seen", {})
    now = datetime.now(timezone.utc).isoformat()

    if not state.get("bootstrapped") and settings.bootstrap_mode == "mark_seen":
        for device_id, candidate in candidates:
            clip_id = stable_clip_id(device_id, candidate)
            seen.setdefault(
                clip_id,
                {
                    "first_seen_at": now,
                    "event_local_time": candidate.event_local_time,
                    "event_types": list(candidate.event_types),
                    "notify": should_notify_candidate(candidate, notification_filter),
                },
            )
        state["bootstrapped"] = True
        save_state(paths.state_file, state)
        return WatchResult(bootstrapped=True, checked_candidates=len(candidates), saved=[], notification_filter=notification_filter)

    saved: list[SavedClip] = []
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    for device_id, candidate in candidates:
        clip_id = stable_clip_id(device_id, candidate)
        if clip_id in seen:
            continue
        out_path = safe_output_path(paths.output_dir, candidate.relative_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not out_path.exists():
            content = TapoCareClient(session).download_bytes(candidate.url)
            out_path.write_bytes(decrypt_tapo_payload(content, candidate.key_b64))
        should_notify = should_notify_candidate(candidate, notification_filter)
        seen[clip_id] = {
            "first_seen_at": now,
            "event_local_time": candidate.event_local_time,
            "event_types": list(candidate.event_types),
            "notify": should_notify,
            "relative_path": candidate.relative_path,
            "size": out_path.stat().st_size if out_path.exists() else None,
        }
        saved.append(SavedClip(candidate.device_alias, candidate.event_local_time, out_path, clip_id, candidate.event_types, should_notify))
    if settings.notify_clips_per_run:
        _enqueue_pending_notifications(state, saved)
        saved = _drain_pending_notifications(state, settings.notify_clips_per_run, settings.notify_queue_policy)
    if settings.attachment_format == "mp4" and settings.max_attachments > 0:
        remuxed: list[SavedClip] = []
        attachment_count = 0
        for clip in saved:
            if clip.notify and attachment_count < settings.max_attachments:
                path = prepare_attachment_path(clip.path, settings.attachment_format)
                remuxed.append(SavedClip(clip.device_alias, clip.event_local_time, path, clip.clip_id, clip.event_types, clip.notify))
                attachment_count += 1
            else:
                remuxed.append(clip)
        saved = remuxed
    combined_attachment = None
    if settings.grid_attachments and settings.max_attachments > 0:
        grid_clips = [clip for clip in saved if clip.notify]
        combined_attachment = prepare_grid_attachment_path(grid_clips, settings.grid_tile_width, settings.grid_tile_height)
    state["bootstrapped"] = True
    save_state(paths.state_file, state)
    return WatchResult(
        bootstrapped=False,
        checked_candidates=len(candidates),
        saved=saved,
        notification_filter=notification_filter,
        combined_attachment=combined_attachment,
    )


def _event_types_label(event_types: tuple[str, ...]) -> str:
    labels = [_EVENT_TYPE_LABELS.get(normalize_event_type(event_type), event_type) for event_type in event_types]
    return ",".join(labels)


def _notification_header(result: WatchResult, notify_count: int) -> str:
    if result.notification_filter == ("PD",):
        return f"📹 Tapo Care人物検知録画: {notify_count}件保存しました。"
    return f"📹 Tapo Care新規録画: {notify_count}件保存しました。"


def format_slack_message(result: WatchResult, max_attachments: int = 3, notify_bootstrap: bool = False) -> str:
    if result.bootstrapped:
        if not notify_bootstrap:
            return ""
        return f"📹 Tapo Care監視を開始しました。既存{result.checked_candidates}件は既読扱いにして、次回以降の新規録画だけ保存・共有します。"
    notify_clips = [clip for clip in result.saved if clip.notify]
    if not notify_clips:
        return ""
    lines = [_notification_header(result, len(notify_clips))]
    attachment_clips = notify_clips[:max_attachments]
    for clip in attachment_clips:
        event_label = _event_types_label(clip.event_types)
        event_part = f" / {event_label}" if event_label else ""
        lines.append(f"- {clip.event_local_time} / {clip.device_alias}{event_part} / {clip.path.name}")
    remaining = len(notify_clips) - max_attachments
    if remaining > 0:
        if result.combined_attachment:
            lines.append(f"ほか{remaining}件もグリッド内に含めて保存済みです。")
        else:
            lines.append(f"ほか{remaining}件はローカルに保存済みです。")
    if result.combined_attachment and len(notify_clips) > 1:
        lines.append(f"MEDIA:{result.combined_attachment}")
    else:
        for clip in attachment_clips:
            lines.append(f"MEDIA:{clip.path}")
    return "\n".join(lines)
