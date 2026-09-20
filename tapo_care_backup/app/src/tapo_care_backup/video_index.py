"""Normalize Tapo Care video-list responses into download candidates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class DownloadCandidate:
    device_alias: str
    event_local_time: str
    url: str
    key_b64: str | None
    relative_path: str
    event_types: tuple[str, ...] = ()


def safe_name(value: str) -> str:
    cleaned = _SAFE_CHARS.sub("_", value.strip()).strip("._")
    return cleaned or "camera"


def _event_path(device_alias: str, event_local_time: str, index: int, url: str) -> str:
    date_part = safe_name(event_local_time[:10]).replace(".", "_").strip("_") if len(event_local_time) >= 10 else "unknown-date"
    date_part = date_part or "unknown-date"
    time_part = safe_name(event_local_time.replace(":", "-").replace(" ", "_")).replace(".", "_").strip("_") or "unknown-time"
    clean_url = url.split("?")[0]
    url_hash = hashlib.sha1(clean_url.encode("utf-8")).hexdigest()[:10]
    return f"{safe_name(device_alias)}/{date_part}/{time_part}_{index}_{url_hash}.ts"


def _normalize_event_type(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _event_types_for_item(item: dict[str, Any]) -> tuple[str, ...]:
    event_types: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        event_type = _normalize_event_type(value)
        if event_type and event_type not in seen:
            seen.add(event_type)
            event_types.append(event_type)

    add(item.get("eventType") or item.get("eventTypeName"))
    for event_type in item.get("eventTypeList") or item.get("event_type_list") or []:
        add(event_type)
    for info in item.get("eventTypeInfos") or item.get("event_type_infos") or []:
        if isinstance(info, dict):
            add(info.get("eventTypeName") or info.get("eventType"))

    return tuple(event_types)


def iter_download_candidates(payload: dict[str, Any], device_alias: str) -> Iterable[DownloadCandidate]:
    """Yield downloadable videos from a `/v1/videos` or `/v2/videos/list` response.

    `encryptionMethod: NONE` and missing `encryptionMethod` are treated as plain
    media. `AES-128-CBC` uses `decryptionInfo.key`.
    """
    for item in payload.get("index", []) or []:
        event_local_time = item.get("eventLocalTime") or item.get("createdLocalTime") or "unknown-time"
        event_types = _event_types_for_item(item)
        videos = item.get("video") or []
        for idx, video in enumerate(videos):
            url = video.get("uri") or video.get("url")
            if not url:
                continue
            method = (video.get("encryptionMethod") or "NONE").upper()
            if method == "AES-128-CBC":
                key_b64 = (video.get("decryptionInfo") or {}).get("key")
                if not key_b64:
                    raise ValueError(f"Encrypted video at {event_local_time} is missing decryptionInfo.key")
            elif method in {"NONE", ""}:
                key_b64 = None
            else:
                raise ValueError(f"Unsupported Tapo Care encryption method: {method}")
            yield DownloadCandidate(device_alias, event_local_time, url, key_b64, _event_path(device_alias, event_local_time, idx, url), event_types)
