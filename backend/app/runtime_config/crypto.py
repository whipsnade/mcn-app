"""Authenticated encryption for tenant runtime secrets.

Only this module deals with plaintext secret values.  Callers persist the
``EncryptedSecretValue`` envelope and pass an exact tenant/secret/kind AAD.
Errors are stable and never include the plaintext or ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class RuntimeConfigError(ValueError):
    """Stable, safe runtime-config failure."""


class EncryptedSecretValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    algorithm: str = Field(default="AES-256-GCM", pattern=r"^AES-256-GCM$")
    nonce: str
    ciphertext: str
    key_version: str
    fingerprint: str
    masked_value: str


class SecretCipher:
    """AES-256-GCM cipher with explicit key versions and random nonces."""

    def __init__(self, *, master_keys: Mapping[str, bytes], active_key_version: str) -> None:
        if not master_keys or active_key_version not in master_keys:
            raise RuntimeConfigError("runtime_secret_key_invalid")
        normalized: dict[str, bytes] = {}
        for version, key in master_keys.items():
            if not isinstance(version, str) or not version or not isinstance(key, bytes) or len(key) != 32:
                raise RuntimeConfigError("runtime_secret_key_invalid")
            normalized[version] = key
        self._master_keys = normalized
        self._active_key_version = active_key_version

    @property
    def active_key_version(self) -> str:
        return self._active_key_version

    @classmethod
    def from_environment(cls, raw: str | None, active_key_version: str) -> SecretCipher:
        if not raw:
            raise RuntimeConfigError("runtime_secret_keys_missing")
        keys: dict[str, bytes] = {}
        try:
            for item in raw.split(","):
                version, encoded = item.split(":", 1)
                keys[version] = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RuntimeConfigError("runtime_secret_keys_invalid") from exc
        return cls(master_keys=keys, active_key_version=active_key_version)

    def encrypt(self, plaintext: SecretStr, *, aad: bytes) -> EncryptedSecretValue:
        if not isinstance(plaintext, SecretStr):
            raise RuntimeConfigError("runtime_secret_plaintext_invalid")
        value = plaintext.get_secret_value()
        if not value:
            raise RuntimeConfigError("runtime_secret_plaintext_invalid")
        key = self._master_keys[self._active_key_version]
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
        return EncryptedSecretValue(
            nonce=_b64(nonce),
            ciphertext=_b64(ciphertext),
            key_version=self._active_key_version,
            fingerprint=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            masked_value="[redacted]",
        )

    def decrypt(self, value: EncryptedSecretValue, *, aad: bytes) -> SecretStr:
        try:
            key = self._master_keys[value.key_version]
            nonce = base64.b64decode(value.nonce, validate=True)
            ciphertext = base64.b64decode(value.ciphertext, validate=True)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError, InvalidTag) as exc:
            raise RuntimeConfigError("runtime_secret_decrypt_failed") from exc
        if hashlib.sha256(plaintext.encode("utf-8")).hexdigest() != value.fingerprint:
            raise RuntimeConfigError("runtime_secret_decrypt_failed")
        return SecretStr(plaintext)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
