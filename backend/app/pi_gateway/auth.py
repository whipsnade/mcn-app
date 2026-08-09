"""HMAC request authentication and per-Run AES-GCM secret envelopes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from .contracts import RuntimeSecretEnvelope


class PiGatewayAuthError(ValueError):
    """Stable error with no request material in its message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedGatewayRequest:
    gateway_id: str
    timestamp: int
    nonce: str


class InMemoryNonceStore:
    """Deterministic nonce store used by pure tests and local fake gateways."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], int] = {}

    def reserve(self, gateway_id: str, nonce: str, expires_at: int, *, now: int) -> bool:
        self._entries = {
            key: expiry for key, expiry in self._entries.items() if expiry > now
        }
        key = (gateway_id, nonce)
        if key in self._entries:
            return False
        self._entries[key] = expires_at
        return True


def build_signature(
    secret: str, method: str, path: str, timestamp: int, nonce: str, body: bytes
) -> str:
    signing = _signing_bytes(method, path, timestamp, nonce, body)
    return hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).hexdigest()


def verify_signed_request(
    headers: Mapping[str, str],
    *,
    method: str,
    path: str,
    body: bytes,
    secret: str,
    allowed_gateway_ids: set[str] | frozenset[str],
    nonce_store: InMemoryNonceStore | None,
    now: int | None = None,
    max_skew_seconds: int = 30,
) -> VerifiedGatewayRequest:
    if not isinstance(secret, str) or not secret:
        raise PiGatewayAuthError("pi_gateway_secret_invalid")
    values = {key.lower(): value for key, value in headers.items()}
    gateway_id = values.get("x-pi-gateway-id")
    raw_timestamp = values.get("x-pi-timestamp")
    nonce = values.get("x-pi-nonce")
    signature = values.get("x-pi-signature")
    if not gateway_id or not raw_timestamp or not nonce or not signature:
        raise PiGatewayAuthError("pi_gateway_headers_invalid")
    if gateway_id not in allowed_gateway_ids:
        raise PiGatewayAuthError("pi_gateway_gateway_unknown")
    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError) as exc:
        raise PiGatewayAuthError("pi_gateway_timestamp_invalid") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        raise PiGatewayAuthError("pi_gateway_timestamp_invalid")
    if not nonce or len(nonce) > 128 or re.fullmatch(r"[A-Za-z0-9._:-]+", nonce) is None:
        raise PiGatewayAuthError("pi_gateway_nonce_invalid")
    expected = build_signature(secret, method, path, timestamp, nonce, body)
    if not hmac.compare_digest(signature, expected):
        raise PiGatewayAuthError("pi_gateway_signature_invalid")
    if nonce_store is not None and not nonce_store.reserve(
        gateway_id, nonce, timestamp + max_skew_seconds, now=current
    ):
        raise PiGatewayAuthError("pi_gateway_nonce_replayed")
    return VerifiedGatewayRequest(gateway_id=gateway_id, timestamp=timestamp, nonce=nonce)


def seal_secret_bundle(
    bundle: Mapping[str, Any],
    *,
    lease_token: str,
    run_id: str,
    attempt_id: str,
    config_version_id: str,
    gateway_id: str,
) -> RuntimeSecretEnvelope:
    key, aad = _envelope_key_and_aad(
        lease_token, run_id, attempt_id, config_version_id, gateway_id
    )
    nonce = os.urandom(12)
    plaintext = json.dumps(dict(bundle), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return RuntimeSecretEnvelope(
        alg="AES-256-GCM",
        nonce=_b64(nonce),
        ciphertext=_b64(ciphertext),
    )


def open_secret_envelope(
    envelope: RuntimeSecretEnvelope,
    *,
    lease_token: str,
    run_id: str,
    attempt_id: str,
    config_version_id: str,
    gateway_id: str,
) -> dict[str, Any]:
    try:
        key, aad = _envelope_key_and_aad(
            lease_token, run_id, attempt_id, config_version_id, gateway_id
        )
        nonce = base64.b64decode(envelope.nonce, validate=True)
        ciphertext = base64.b64decode(envelope.ciphertext, validate=True)
        value = json.loads(AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception as exc:
        raise PiGatewayAuthError("pi_gateway_secret_envelope_invalid") from exc


def _signing_bytes(method: str, path: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode("utf-8")


def _envelope_key_and_aad(
    lease_token: str, run_id: str, attempt_id: str, config_version_id: str, gateway_id: str
) -> tuple[bytes, bytes]:
    aad = f"{run_id}:{attempt_id}:{config_version_id}:{gateway_id}".encode("utf-8")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"pi-gateway-secret-v1",
        info=b"lease:" + aad,
    ).derive(lease_token.encode("utf-8"))
    return key, aad


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
