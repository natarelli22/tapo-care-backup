"""Command-line interface for tapo-care-backup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .api import TapoApiError, TapoCareClient, TapoCloudClient, login_from_env_or_prompt
from .config import DEFAULT_CONFIG_PATH, load_session, save_session
from .region import region_from_app_server_url
from .crypto import decrypt_tapo_payload
from .time_window import build_time_window
from .video_index import iter_download_candidates, safe_name
from .monitor import safe_output_path


def _convert_ts_to_mp4(ts_path: Path, mp4_path: Path) -> bool:
    """Remux video stream and re-encode audio to AAC for browser compatibility."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print(f"Warning: ffmpeg not found in PATH; keeping {ts_path}", file=sys.stderr)
        return False

    tmp_mp4 = mp4_path.with_suffix(".tmp.mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(ts_path),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(tmp_mp4),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode == 0 and tmp_mp4.exists() and tmp_mp4.stat().st_size > 0:
            tmp_mp4.replace(mp4_path)
            ts_path.unlink(missing_ok=True)
            return True
        else:
            print(f"ffmpeg conversion failed for {ts_path}: {res.stderr.strip()}", file=sys.stderr)
            tmp_mp4.unlink(missing_ok=True)
            return False
    except Exception as exc:
        print(f"ffmpeg conversion error for {ts_path}: {exc}", file=sys.stderr)
        tmp_mp4.unlink(missing_ok=True)
        return False


def _generate_thumbnail(video_path: Path, thumb_path: Path) -> bool:
    """Extract the first video frame and save as high quality JPEG thumbnail."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False

    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_thumb = thumb_path.with_suffix(".tmp.jpg")
    cmd = [
        ffmpeg,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "00:00:00",
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(tmp_thumb),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and tmp_thumb.exists() and tmp_thumb.stat().st_size > 0:
            tmp_thumb.replace(thumb_path)
            return True
        else:
            tmp_thumb.unlink(missing_ok=True)
            return False
    except Exception:
        tmp_thumb.unlink(missing_ok=True)
        return False


def _parse_camera_paths(raw: str | None) -> dict[str, Path]:
    """Parse camera destination paths from JSON string or file path."""
    if not raw or not raw.strip():
        return {}
    try:
        p = Path(raw)
        if p.exists() and p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            data = json.loads(raw)
        result: dict[str, Path] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item and "path" in item:
                    result[item["name"].strip().lower()] = Path(item["path"].strip())
        elif isinstance(data, dict):
            for k, v in data.items():
                result[k.strip().lower()] = Path(str(v).strip())
        return result
    except Exception as exc:
        print(f"Warning: Could not parse camera paths ({exc})", file=sys.stderr)
        return {}


def _resolve_camera_base_dir(alias: str, device_id: str, camera_paths: dict[str, Path], default_path: Path) -> Path:
    alias_key = alias.strip().lower()
    dev_key = device_id.strip().lower()
    safe_alias_key = safe_name(alias).lower()

    if alias_key in camera_paths:
        return camera_paths[alias_key]
    if safe_alias_key in camera_paths:
        return camera_paths[safe_alias_key]
    if dev_key in camera_paths:
        return camera_paths[dev_key]
    return safe_output_path(default_path, safe_name(alias))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapo-care-backup", description="Back up Tapo Care cloud recordings for your own TP-Link/Tapo account.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Session cache path")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Log in and cache a TP-Link cloud token")
    login.add_argument("--auth-mode", choices=["legacy", "signed"], default="legacy", help="legacy avoids mobile-app client signing; signed supports MFA if client keys are provided")
    login.add_argument("--strict-tls", action="store_true", help="Verify TLS certificates for cloud login when possible")

    devices = sub.add_parser("devices", help="List account cameras")
    devices.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    list_cmd = sub.add_parser("list", help="List Tapo Care cloud videos")
    _add_video_filters(list_cmd)
    list_cmd.add_argument("--json", action="store_true", help="Print raw JSON responses")

    download = sub.add_parser("download", help="Download Tapo Care cloud videos")
    _add_video_filters(download)
    download.add_argument("--path", type=Path, default=Path("backups"), help="Default base output directory")
    download.add_argument("--camera-paths", default="", help="JSON mapping or file path with camera paths")
    download.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    download.add_argument("--to-mp4", action="store_true", default=True, help="Convert downloaded .ts files to .mp4 and delete raw .ts")
    download.add_argument("--no-to-mp4", dest="to_mp4", action="store_false", help="Keep raw .ts files without converting to .mp4")
    download.add_argument("--generate-thumb", action="store_true", default=True, help="Extract first video frame as JPEG thumbnail in thumbs folder")
    download.add_argument("--no-thumb", dest="generate_thumb", action="store_false", help="Do not generate thumbnails")
    download.add_argument("--date-subfolders", action="store_true", default=True, help="Organize videos and thumbs in YYYY-MM-DD subfolders")
    download.add_argument("--no-date-subfolders", dest="date_subfolders", action="store_false", help="Save videos and thumbs flat in videos/ and thumbs/ without date subfolders")

    doctor = sub.add_parser("doctor", help="Probe Tapo Care API endpoint without credentials")
    doctor.add_argument("--region", default="aps1")

    return parser


def _add_video_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device-id", help="Only use a specific Tapo deviceId")
    parser.add_argument("--days", type=int, default=1, help="Number of previous local days to include, plus today")
    parser.add_argument("--timezone", default=None, help="Timezone used for date boundaries (defaults to system / Home Assistant timezone)")
    parser.add_argument("--page-size", type=int, default=3000)


def _camera_devices(session_path: Path):
    try:
        session = load_session(session_path)
    except FileNotFoundError:
        if os.environ.get("TAPO_USERNAME") and os.environ.get("TAPO_PASSWORD"):
            session = login_from_env_or_prompt()
            save_session(session, session_path)
        else:
            raise
    cloud = TapoCloudClient()
    try:
        return [d for d in cloud.list_devices(session) if d.device_type == "SMART.IPCAMERA" or "CAMERA" in d.device_type.upper()]
    except TapoApiError:
        if os.environ.get("TAPO_USERNAME") and os.environ.get("TAPO_PASSWORD"):
            session = login_from_env_or_prompt()
            save_session(session, session_path)
            return [d for d in cloud.list_devices(session) if d.device_type == "SMART.IPCAMERA" or "CAMERA" in d.device_type.upper()]
        raise


def cmd_login(args: argparse.Namespace) -> int:
    session = login_from_env_or_prompt(auth_mode=args.auth_mode, verify_tls=args.strict_tls)
    save_session(session, args.config)
    print(f"Saved session for {session.email} ({session.region}) to {args.config}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    devices = _camera_devices(args.config)
    if args.json:
        print(json.dumps([d.__dict__ for d in devices], indent=2, ensure_ascii=False))
    else:
        for d in devices:
            print(f"{d.device_id}\t{d.alias}\t{d.model or ''}\t{d.device_type}")
    return 0


def _selected_devices(config_path: Path, device_id: str | None):
    try:
        session = load_session(config_path)
    except FileNotFoundError:
        if os.environ.get("TAPO_USERNAME") and os.environ.get("TAPO_PASSWORD"):
            session = login_from_env_or_prompt()
            save_session(session, config_path)
        else:
            raise
    devices = _camera_devices(config_path)
    for d in devices:
        if d.app_server_url:
            cam_region = region_from_app_server_url(d.app_server_url)
            if cam_region and cam_region != session.region:
                print(f"[INFO] Updating session region from camera '{d.alias}' appServerUrl ({d.app_server_url}): {session.region} -> {cam_region}")
                from dataclasses import replace as _dc_replace
                session = _dc_replace(session, region=cam_region)
                save_session(session, config_path)
                break
    if device_id:
        return session, [(device_id, device_id)]
    return session, [(d.device_id, d.alias) for d in devices]


def cmd_list(args: argparse.Namespace) -> int:
    session, devices = _selected_devices(args.config, args.device_id)
    care = TapoCareClient(session)
    start, end = build_time_window(args.days, args.timezone)
    raw = {}
    for device_id, alias in devices:
        pages = list(care.iter_video_pages(device_id, start, end, page_size=args.page_size))
        # Persist discovered region so future runs skip the probe.
        if care.discovered_region and care.discovered_region != session.region:
            from dataclasses import replace as _dc_replace
            session = _dc_replace(session, region=care.discovered_region)
            save_session(session, args.config)
            print(f"[INFO] Session updated with discovered region '{care.discovered_region}'.")
        raw[device_id] = pages
        if not args.json:
            total = pages[0].get("total", 0) if pages else 0
            print(f"{alias}: {total} videos across {len(pages)} page(s)")
            for payload in pages:
                for candidate in iter_download_candidates(payload, alias):
                    print(f"  {candidate.event_local_time}\t{candidate.relative_path}\t{candidate.url}")
    if args.json:
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    session, devices = _selected_devices(args.config, args.device_id)
    camera_paths = _parse_camera_paths(args.camera_paths)
    care = TapoCareClient(session)
    start, end = build_time_window(args.days, args.timezone)
    downloaded = 0
    skipped = 0
    for device_id, alias in devices:
        try:
            pages = list(care.iter_video_pages(device_id, start, end, page_size=args.page_size))
        except TapoApiError as api_err:
            if os.environ.get("TAPO_USERNAME") and os.environ.get("TAPO_PASSWORD"):
                print(f"[WARN] Error listing videos ({api_err}). Refreshing session and retrying...", flush=True)
                session = login_from_env_or_prompt()
                save_session(session, args.config)
                care = TapoCareClient(session)
                pages = list(care.iter_video_pages(device_id, start, end, page_size=args.page_size))
            else:
                raise

        # If region probing found a better region, persist it so future runs skip the probe.
        if care.discovered_region and care.discovered_region != session.region:
            from dataclasses import replace as _dc_replace
            session = _dc_replace(session, region=care.discovered_region)
            save_session(session, args.config)
            print(f"[INFO] Session updated with discovered region '{care.discovered_region}'.")

        camera_base = _resolve_camera_base_dir(alias, device_id, camera_paths, args.path)
        videos_base = camera_base / "videos"
        thumbs_base = camera_base / "thumbs"
        videos_base.mkdir(parents=True, exist_ok=True)
        thumbs_base.mkdir(parents=True, exist_ok=True)

        for payload in pages:
            for candidate in iter_download_candidates(payload, alias):
                rel_parts = Path(candidate.relative_path).parts
                date_part = rel_parts[1] if len(rel_parts) >= 2 else (candidate.event_local_time[:10].replace(":", "-").replace(".", "-").replace(" ", "_") if len(candidate.event_local_time) >= 10 else "unknown-date")
                file_stem = Path(candidate.relative_path).stem

                v_dir = videos_base / date_part if args.date_subfolders else videos_base
                t_dir = thumbs_base / date_part if args.date_subfolders else thumbs_base
                v_dir.mkdir(parents=True, exist_ok=True)
                t_dir.mkdir(parents=True, exist_ok=True)

                mp4_path = v_dir / f"{file_stem}.mp4" if args.to_mp4 else None
                ts_path = v_dir / f"{file_stem}.ts"
                thumb_path = t_dir / f"{file_stem}.jpg"

                stem_prefix = file_stem.rsplit("_", 1)[0]

                if not args.overwrite:
                    existing_mp4_matches = sorted([
                        f for f in v_dir.glob(f"{stem_prefix}_*.mp4")
                        if f.is_file() and f.stat().st_size > 0
                    ])
                    if mp4_path and mp4_path.exists() and mp4_path.stat().st_size > 0 and mp4_path not in existing_mp4_matches:
                        existing_mp4_matches.append(mp4_path)

                    if existing_mp4_matches:
                        main_file = existing_mp4_matches[0]
                        # Clean up duplicate files from previous runs if URL hashes differed
                        if len(existing_mp4_matches) > 1:
                            for dup in existing_mp4_matches[1:]:
                                dup.unlink(missing_ok=True)
                                (t_dir / f"{dup.stem}.jpg").unlink(missing_ok=True)

                        main_thumb = t_dir / f"{main_file.stem}.jpg"
                        if args.generate_thumb and not (main_thumb.exists() and main_thumb.stat().st_size > 0):
                            _generate_thumbnail(main_file, main_thumb)

                        skipped += 1
                        continue

                    existing_ts_matches = [
                        f for f in v_dir.glob(f"{stem_prefix}_*.ts")
                        if f.is_file() and f.stat().st_size > 0
                    ]
                    if existing_ts_matches:
                        if args.to_mp4:
                            target_ts = existing_ts_matches[0]
                            target_mp4 = target_ts.with_suffix(".mp4")
                            if _convert_ts_to_mp4(target_ts, target_mp4):
                                if args.generate_thumb:
                                    _generate_thumbnail(target_mp4, t_dir / f"{target_mp4.stem}.jpg")
                            for dup_ts in existing_ts_matches[1:]:
                                dup_ts.unlink(missing_ok=True)
                            skipped += 1
                            continue
                        skipped += 1
                        continue

                tmp_ts = ts_path.with_suffix(".tmp.ts")
                content = care.download_bytes(candidate.url)
                tmp_ts.write_bytes(decrypt_tapo_payload(content, candidate.key_b64))
                tmp_ts.replace(ts_path)

                target_video = ts_path
                if args.to_mp4 and mp4_path:
                    if _convert_ts_to_mp4(ts_path, mp4_path):
                        target_video = mp4_path
                    else:
                        print(f"Warning: mp4 conversion failed for {ts_path.name}, keeping .ts")

                if args.generate_thumb and target_video.exists():
                    _generate_thumbnail(target_video, thumb_path)

                downloaded += 1
                thumb_info = f" (thumb: {thumb_path.name})" if args.generate_thumb and thumb_path.exists() else ""
                print(f"downloaded & processed {target_video.name}{thumb_info}")

    print(f"done: downloaded={downloaded} skipped={skipped}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import requests

    url = f"https://{args.region}-app-tapo-care.i.tplinknbu.com/v2/videos/list"
    response = requests.get(url, timeout=15, verify=False)
    print(f"{url} -> HTTP {response.status_code} {response.text[:160]}")
    return 0 if response.status_code in {401, 403} else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            return cmd_login(args)
        if args.command == "devices":
            return cmd_devices(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "download":
            return cmd_download(args)
        if args.command == "doctor":
            return cmd_doctor(args)
    except FileNotFoundError as exc:
        print(f"No saved session. Run `tapo-care-backup login` first. ({exc})", file=sys.stderr)
        return 2
    except TapoApiError as exc:
        print(f"Tapo API error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
