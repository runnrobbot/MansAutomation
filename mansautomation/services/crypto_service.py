"""AES-GCM-based local encryption for profile data and secrets."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mansautomation.core.exceptions import CryptoError

_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_HEADER = b"MANS1"  # versioned envelope header


class CryptoService:
    """Manages a per-installation symmetric key stored on disk.

    The keystore lives in the user's data directory with restrictive permissions.
    All encrypt/decrypt calls produce versioned, authenticated envelopes:

        [HEADER(5)][NONCE(12)][CIPHERTEXT][TAG(16)]
    """

    def __init__(self, keystore_path: Path) -> None:
        self._keystore_path = keystore_path
        self._key = self._load_or_create_key()
        self._cipher = AESGCM(self._key)

    def _load_or_create_key(self) -> bytes:
        if self._keystore_path.exists():
            try:
                key = self._keystore_path.read_bytes()
            except OSError as exc:
                raise CryptoError(f"failed to read keystore: {exc}") from exc
            if len(key) != _KEY_LENGTH:
                raise CryptoError("keystore is corrupted (invalid key length)")
            return key
        key = AESGCM.generate_key(bit_length=256)
        self._keystore_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._keystore_path, "wb") as fh:
                fh.write(key)
            try:
                os.chmod(self._keystore_path, 0o600)
            except OSError:
                # Windows / restricted FS - ignore but keystore is still local
                pass
        except OSError as exc:
            raise CryptoError(f"failed to create keystore: {exc}") from exc
        return key

    def encrypt(self, plaintext: bytes, *, associated_data: bytes | None = None) -> bytes:
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        try:
            ciphertext = self._cipher.encrypt(nonce, plaintext, associated_data)
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"encryption failed: {exc}") from exc
        return _HEADER + nonce + ciphertext

    def decrypt(self, payload: bytes, *, associated_data: bytes | None = None) -> bytes:
        if len(payload) < len(_HEADER) + _NONCE_LENGTH + 16:
            raise CryptoError("ciphertext is too short")
        if payload[: len(_HEADER)] != _HEADER:
            raise CryptoError("unknown ciphertext envelope")
        nonce = payload[len(_HEADER) : len(_HEADER) + _NONCE_LENGTH]
        ciphertext = payload[len(_HEADER) + _NONCE_LENGTH :]
        try:
            return self._cipher.decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise CryptoError("ciphertext authentication failed") from exc
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"decryption failed: {exc}") from exc
