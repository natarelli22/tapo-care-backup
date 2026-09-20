"""Tapo Care media decryption helpers."""
from __future__ import annotations

import base64

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def decrypt_tapo_payload(content: bytes, key_b64: str | None) -> bytes:
    """Decrypt a Tapo Care AES-128-CBC payload, or return plain bytes.

    Tapo Care encrypted MP4 payloads observed in the mobile API are shaped as:
    first 16 bytes IV + remaining bytes AES-CBC ciphertext with PKCS#7 padding.
    """
    if not key_b64:
        return content
    if len(content) < AES.block_size:
        raise ValueError("Encrypted payload is too short to contain an IV")
    key = base64.b64decode(key_b64)
    iv = content[: AES.block_size]
    ciphertext = content[AES.block_size :]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ciphertext), AES.block_size)
