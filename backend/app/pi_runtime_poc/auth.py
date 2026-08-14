"""Pi RPC POC 的运行环境门禁与单 Run 临时令牌。"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException, status

from app.core.config import Settings, get_settings

_POC_DATABASE = "kol_insight_pi_poc"
_UNAUTHORIZED_DETAIL = "invalid_pi_poc_token"


class PiPocSettingsGuard:
    """拒绝任何未明确隔离到 POC 数据库的 Pi 运行。"""

    @staticmethod
    def assert_safe(settings: Settings) -> None:
        if not settings.pi_runtime_poc_enabled:
            raise RuntimeError("pi_poc_disabled")
        if settings.app_env != "test":
            raise RuntimeError("pi_poc_test_environment_required")
        if settings.mysql_database != _POC_DATABASE:
            raise RuntimeError("pi_poc_database_required")

        secret = settings.pi_runtime_poc_internal_secret
        if secret is None or not secret.get_secret_value().strip():
            raise RuntimeError("pi_poc_internal_secret_required")


def issue_run_token(run_id: str, *, settings: Settings | None = None, now: int | None = None) -> str:
    """签发只可用于指定 Run 的短期内部令牌。"""

    config = settings or get_settings()
    PiPocSettingsGuard.assert_safe(config)
    if not run_id:
        raise ValueError("run_id must not be blank")

    issued_at = int(time.time()) if now is None else now
    payload = {
        "run_id": run_id,
        "exp": issued_at + config.pi_runtime_poc_run_timeout_seconds,
        "nonce": secrets.token_urlsafe(24),
    }
    encoded_payload = _encode_json(payload)
    signature = _sign(encoded_payload, config)
    return f"{encoded_payload}.{_encode_bytes(signature)}"


def verify_run_token(
    token: str,
    run_id: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> str:
    """验证令牌并返回其已签名的 Run 身份；任一失败均为 401。"""

    config = settings or get_settings()
    PiPocSettingsGuard.assert_safe(config)
    try:
        encoded_payload, encoded_signature = token.split(".")
        expected_signature = _sign(encoded_payload, config)
        supplied_signature = _decode_bytes(encoded_signature)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature")

        payload = _decode_json(encoded_payload)
        if set(payload) != {"run_id", "exp", "nonce"}:
            raise ValueError("claims")
        token_run_id = payload["run_id"]
        expires_at = payload["exp"]
        nonce = payload["nonce"]
        if (
            not isinstance(token_run_id, str)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(nonce, str)
            or not nonce
            or not hmac.compare_digest(token_run_id, run_id)
            or expires_at <= (int(time.time()) if now is None else now)
        ):
            raise ValueError("claims")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise _unauthorized() from None

    return token_run_id


def _sign(encoded_payload: str, settings: Settings) -> bytes:
    secret = settings.pi_runtime_poc_internal_secret
    assert secret is not None
    return hmac.new(
        secret.get_secret_value().encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _encode_json(payload: dict[str, Any]) -> str:
    return _encode_bytes(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _decode_json(encoded_payload: str) -> dict[str, Any]:
    value = json.loads(_decode_bytes(encoded_payload))
    if not isinstance(value, dict):
        raise TypeError("payload")
    return value


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )
