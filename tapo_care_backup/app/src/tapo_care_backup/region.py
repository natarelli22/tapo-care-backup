"""Region helpers for TP-Link/Tapo NBU endpoints."""
from __future__ import annotations

import re

DEFAULT_REGION = "aps1"

# Matches patterns like:
#   https://aps1-app-server.iot.i.tplinknbu.com
#   https://use1-app-cloudgateway.tplinkcloud.com
#   https://eu-wap.tplinkcloud.com
#   https://use1-wap.tplinkcloud.com
_REGION_RE = re.compile(r"https?://([a-z0-9]+)-(?:app-(?:server|cloudgateway)|wap)\.")

# Mapping from WAP/server subdomains to Tapo Care NBU regions.
REGION_MAP: dict[str, str] = {
    "eu": "euw1",
    "euw1": "euw1",
    "use1": "use1",
    "aps1": "aps1",
    "us": "use1",
    "apac": "aps1",
}

# The only valid Tapo Care endpoints that exist globally:
#   use1 (US East / Americas)
#   euw1 (Europe West)
#   aps1 (Asia-Pacific South)
KNOWN_REGIONS: list[str] = ["use1", "euw1", "aps1"]


def region_from_app_server_url(app_server_url: str | None, default: str = DEFAULT_REGION) -> str:
    """Extract `aps1`/`euw1`/`use1` from a TP-Link app server URL.

    Handles patterns such as:
      - https://eu-wap.tplinkcloud.com -> euw1
      - https://use1-wap.tplinkcloud.com -> use1
      - https://aps1-app-server.iot.i.tplinknbu.com -> aps1
      - https://use1-app-cloudgateway.tplinkcloud.com -> use1
    Falls back to *default* when no region can be parsed.
    """
    if not app_server_url:
        return default
    match = _REGION_RE.search(app_server_url)
    if not match:
        return default
    raw_region = match.group(1).lower()
    return REGION_MAP.get(raw_region, raw_region)


def care_base_url(region: str | None = None) -> str:
    """Return the regional Tapo Care app API base URL."""
    return f"https://{region or DEFAULT_REGION}-app-tapo-care.i.tplinknbu.com"


def app_server_url_for_region(region: str | None = None) -> str:
    """Return a best-effort regional app server URL for account/device APIs."""
    return f"https://{region or DEFAULT_REGION}-app-server.iot.i.tplinknbu.com"
