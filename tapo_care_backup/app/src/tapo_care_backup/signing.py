"""Request signing helpers for TP-Link's newer mobile API.

These helpers implement the public HMAC shape used by the Tapo mobile app.
They intentionally accept the access key and client secret as arguments so
user/account secrets never need to be committed to this repository.
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def content_md5(content: str) -> str:
    """Return the base64-encoded MD5 digest required by Content-Md5."""
    return base64.b64encode(hashlib.md5(content.encode("utf-8")).digest()).decode("utf-8")


def signature(content: str, endpoint: str, timestamp: str, nonce: str, client_secret: str) -> str:
    """Return the lowercase HMAC-SHA1 signature for a signed Tapo request."""
    payload = "\n".join([content_md5(content), timestamp, nonce, endpoint]).encode("utf-8")
    return hmac.new(client_secret.encode("utf-8"), payload, hashlib.sha1).digest().hex()


def x_authorization(
    content: str,
    endpoint: str,
    timestamp: str,
    nonce: str,
    access_key: str,
    client_secret: str,
) -> str:
    """Build the X-Authorization header value."""
    sig = signature(content, endpoint, timestamp, nonce, client_secret)
    return f"Timestamp={timestamp}, Nonce={nonce}, AccessKey={access_key}, Signature={sig}"
