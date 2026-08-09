import time

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.pi_gateway.auth import (
    InMemoryNonceStore,
    PiGatewayAuthError,
    build_signature,
    open_secret_envelope,
    seal_secret_bundle,
    verify_signed_request,
)


SECRET = "gateway-test-secret"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mysql_password": SecretStr("test-password"),
        "jwt_secret": SecretStr("j" * 32),
        "tencent_plan_api_key": SecretStr("test-model-key"),
        "datatap_mcp_token": SecretStr("test-datatap-token"),
    }
    values.update(overrides)
    return Settings(**values)


def _headers(method="POST", path="/api/v1/internal/pi-gateway/v1/claims", body=b"{}", *, nonce="n-1", timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    return {
        "X-Pi-Gateway-Id": "gw-test",
        "X-Pi-Timestamp": str(timestamp),
        "X-Pi-Nonce": nonce,
        "X-Pi-Signature": build_signature(SECRET, method, path, timestamp, nonce, body),
    }


def test_signature_binds_method_path_timestamp_nonce_and_body() -> None:
    body = b'{"run_id":"run-1"}'
    headers = _headers(body=body)
    verified = verify_signed_request(
        headers,
        method="POST",
        path="/api/v1/internal/pi-gateway/v1/claims",
        body=body,
        secret=SECRET,
        allowed_gateway_ids={"gw-test"},
        nonce_store=InMemoryNonceStore(),
    )
    assert verified.gateway_id == "gw-test"

    for kwargs in (
        {"method": "GET"},
        {"path": "/other"},
        {"body": b"tampered"},
    ):
        altered = dict(headers)
        with pytest.raises(PiGatewayAuthError) as exc_info:
            verify_signed_request(
                altered,
                method=kwargs.get("method", "POST"),
                path=kwargs.get("path", "/api/v1/internal/pi-gateway/v1/claims"),
                body=kwargs.get("body", body),
                secret=SECRET,
                allowed_gateway_ids={"gw-test"},
                nonce_store=InMemoryNonceStore(),
            )
        assert exc_info.value.code == "pi_gateway_signature_invalid"


def test_signature_rejects_old_timestamp_unknown_gateway_and_replayed_nonce() -> None:
    store = InMemoryNonceStore()
    now = int(time.time())
    with pytest.raises(PiGatewayAuthError, match="pi_gateway_timestamp_invalid"):
        verify_signed_request(
            _headers(timestamp=now - 31),
            method="POST",
            path="/api/v1/internal/pi-gateway/v1/claims",
            body=b"{}",
            secret=SECRET,
            allowed_gateway_ids={"gw-test"},
            nonce_store=store,
            now=now,
        )
    unknown = _headers(nonce="n-unknown")
    unknown["X-Pi-Gateway-Id"] = "gw-unknown"
    unknown["X-Pi-Signature"] = build_signature(
        SECRET, "POST", "/api/v1/internal/pi-gateway/v1/claims", now, "n-unknown", b"{}"
    )
    with pytest.raises(PiGatewayAuthError) as exc_info:
        verify_signed_request(
            unknown,
            method="POST",
            path="/api/v1/internal/pi-gateway/v1/claims",
            body=b"{}",
            secret=SECRET,
            allowed_gateway_ids={"gw-test"},
            nonce_store=store,
            now=now,
        )
    assert exc_info.value.code == "pi_gateway_gateway_unknown"

    headers = _headers(nonce="n-replay", timestamp=now)
    verify_signed_request(
        headers,
        method="POST",
        path="/api/v1/internal/pi-gateway/v1/claims",
        body=b"{}",
        secret=SECRET,
        allowed_gateway_ids={"gw-test"},
        nonce_store=store,
        now=now,
    )
    with pytest.raises(PiGatewayAuthError) as exc_info:
        verify_signed_request(
            headers,
            method="POST",
            path="/api/v1/internal/pi-gateway/v1/claims",
            body=b"{}",
            secret=SECRET,
            allowed_gateway_ids={"gw-test"},
            nonce_store=store,
            now=now,
        )
    assert exc_info.value.code == "pi_gateway_nonce_replayed"


def test_signature_rejects_blank_secret_and_control_character_nonce() -> None:
    with pytest.raises(PiGatewayAuthError) as exc_info:
        verify_signed_request(
            _headers(),
            method="POST",
            path="/api/v1/internal/pi-gateway/v1/claims",
            body=b"{}",
            secret="",
            allowed_gateway_ids={"gw-test"},
            nonce_store=InMemoryNonceStore(),
        )
    assert exc_info.value.code == "pi_gateway_secret_invalid"

    headers = _headers(nonce="bad\nnonce")
    with pytest.raises(PiGatewayAuthError) as exc_info:
        verify_signed_request(
            headers,
            method="POST",
            path="/api/v1/internal/pi-gateway/v1/claims",
            body=b"{}",
            secret=SECRET,
            allowed_gateway_ids={"gw-test"},
            nonce_store=InMemoryNonceStore(),
        )
    assert exc_info.value.code == "pi_gateway_nonce_invalid"


def test_signature_verification_can_defer_nonce_reservation_to_database() -> None:
    headers = _headers(nonce="db-only")
    verified = verify_signed_request(
        headers,
        method="POST",
        path="/api/v1/internal/pi-gateway/v1/claims",
        body=b"{}",
        secret=SECRET,
        allowed_gateway_ids={"gw-test"},
        nonce_store=None,
    )
    assert verified.nonce == "db-only"


def test_secret_envelope_is_aad_bound_and_never_serializes_plaintext() -> None:
    bundle = {
        "model_base_url": "https://model.invalid",
        "model_api_key": "model-secret",
        "datatap_token": "datatap-secret",
        "datatap_urls": {"insight-cube": "https://cube.invalid"},
    }
    envelope = seal_secret_bundle(
        bundle,
        lease_token="lease-1",
        run_id="run-1",
        attempt_id="attempt-1",
        config_version_id="config-1",
        gateway_id="gw-test",
    )
    encoded = envelope.model_dump_json()
    assert "model-secret" not in encoded
    assert "datatap-secret" not in encoded
    assert open_secret_envelope(
        envelope,
        lease_token="lease-1",
        run_id="run-1",
        attempt_id="attempt-1",
        config_version_id="config-1",
        gateway_id="gw-test",
    ) == bundle
    with pytest.raises(PiGatewayAuthError) as exc_info:
        open_secret_envelope(
            envelope,
            lease_token="lease-1",
            run_id="run-other",
            attempt_id="attempt-1",
            config_version_id="config-1",
            gateway_id="gw-test",
        )
    assert exc_info.value.code == "pi_gateway_secret_envelope_invalid"


def test_control_plane_http_is_loopback_only_and_production_requires_https() -> None:
    with pytest.raises(ValidationError):
        _settings(app_env="production", pi_gateway_control_plane_url="http://gateway.invalid")
    with pytest.raises(ValidationError):
        _settings(app_env="development", pi_gateway_control_plane_url="http://gateway.invalid")
    assert str(
        _settings(app_env="test", pi_gateway_control_plane_url="http://127.0.0.1:8080").pi_gateway_control_plane_url
    ).startswith("http://127.0.0.1")
