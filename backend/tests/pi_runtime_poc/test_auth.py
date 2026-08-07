import base64
import json
import secrets
import time

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core.config import Settings
from app.pi_runtime_poc.auth import issue_run_token, verify_run_token
from app.pi_runtime_poc.schemas import PiInternalToolRequest


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        mysql_database="kol_insight_pi_poc",
        mysql_password=SecretStr(secrets.token_urlsafe(24)),
        jwt_secret=SecretStr(secrets.token_urlsafe(32)),
        tencent_plan_api_key=SecretStr(secrets.token_urlsafe(24)),
        datatap_mcp_token=SecretStr(secrets.token_urlsafe(24)),
        pi_runtime_poc_enabled=True,
        pi_runtime_poc_internal_secret=SecretStr(secrets.token_urlsafe(32)),
    )


def test_run_token_verifies_only_its_own_run(settings: Settings) -> None:
    run_id = "run-a"
    token = issue_run_token(run_id, settings=settings)

    assert verify_run_token(token, run_id, settings=settings) == run_id

    with pytest.raises(HTTPException) as error:
        verify_run_token(token, "run-b", settings=settings)

    assert error.value.status_code == 401
    assert error.value.detail == "invalid_pi_poc_token"


def test_run_token_rejects_tampering_with_unauthorized(settings: Settings) -> None:
    token = issue_run_token("run-a", settings=settings)
    payload, signature = token.split(".")
    altered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]

    with pytest.raises(HTTPException) as error:
        verify_run_token(f"{altered_payload}.{signature}", "run-a", settings=settings)

    assert error.value.status_code == 401
    assert error.value.detail == "invalid_pi_poc_token"


def test_run_token_rejects_expiry_with_unauthorized(settings: Settings) -> None:
    expired_settings = settings.model_copy(update={"pi_runtime_poc_run_timeout_seconds": 1})
    token = issue_run_token("run-a", settings=expired_settings, now=int(time.time()) - 2)

    with pytest.raises(HTTPException) as error:
        verify_run_token(token, "run-a", settings=expired_settings)

    assert error.value.status_code == 401
    assert error.value.detail == "invalid_pi_poc_token"


def test_run_token_payload_contains_only_run_expiry_and_nonce(settings: Settings) -> None:
    token = issue_run_token("run-a", settings=settings)
    encoded_payload = token.split(".")[0]
    decoded_payload = base64.urlsafe_b64decode(encoded_payload + "==")

    assert set(json.loads(decoded_payload)) == {"run_id", "exp", "nonce"}


def test_pi_internal_tool_request_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        PiInternalToolRequest.model_validate(
            {"tool_name": "read_history", "arguments": {}, "unexpected": True}
        )
