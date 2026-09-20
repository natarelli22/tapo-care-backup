"""Local session cache helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TAPO_CARE_BACKUP_CONFIG_DIR", Path.home() / ".config" / "tapo-care-backup"))
DEFAULT_CONFIG_PATH = CONFIG_DIR / "session.json"


@dataclass(frozen=True)
class StoredSession:
    token: str
    email: str
    region: str
    app_server_url: str | None = None


def save_session(session: StoredSession, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist a session token with user-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(session), indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def load_session(path: Path = DEFAULT_CONFIG_PATH) -> StoredSession:
    """Load a cached session token."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return StoredSession(
        token=data["token"],
        email=data.get("email", ""),
        region=data.get("region", "aps1"),
        app_server_url=data.get("app_server_url"),
    )
