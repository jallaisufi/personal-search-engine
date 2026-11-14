"""Encryption for secrets stored in the database (OAuth refresh tokens).

Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from ENCRYPTION_KEY. A database
dump or backup alone is then not enough to reach someone's Google Drive.
"""

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class DecryptionError(RuntimeError):
    """The stored secret can't be read: ENCRYPTION_KEY changed since it was saved."""


class Cipher:
    def __init__(self, key: str):
        if len(key) < 16:
            raise ValueError("ENCRYPTION_KEY must be at least 16 characters")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def encrypt(self, value: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(value).encode())

    def decrypt(self, token: bytes) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(bytes(token)))
        except InvalidToken as exc:
            raise DecryptionError(
                "Stored credentials can't be decrypted. ENCRYPTION_KEY has changed; "
                "reconnect the source."
            ) from exc
