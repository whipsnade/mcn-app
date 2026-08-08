"""Task 8B：Shell、Settings 与 Pi 的 DataTap 映射桥接回归测试。"""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

from app.agent_runtime.models import AgentRun
from app.core.config import Settings
from app.pi_runtime_poc import comparison

_SERVICE_MAPPING = {
    "bilibili-mcp": "https://datatap.example.test/bilibili/mcp",
    "insight-cube-mcp": "https://datatap.example.test/insight/mcp",
    "social-grow-content-mcp": "https://datatap.example.test/content/mcp",
    "social-grow-mcp": "https://datatap.example.test/social/mcp",
}


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATATAP_MCP_URLS", json.dumps(_SERVICE_MAPPING))
    return Settings(
        mysql_password=SecretStr("test-only-password"),
        jwt_secret=SecretStr("test-only-jwt-secret-at-least-32-characters"),
        tencent_plan_api_key=SecretStr("test-only-model-key"),
        datatap_mcp_token=SecretStr("test-only-datatap-token"),
        tencent_plan_reasoning_effort="high",
    )


def _load_runner_module():
    script = Path(__file__).parents[2] / "scripts" / "run_pi_runtime_poc.py"
    spec = importlib.util.spec_from_file_location("pi_runtime_poc_task8b_runner", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shell_normalizes_external_endpoint_json_for_python_settings() -> None:
    helper = Path(__file__).parents[2] / "scripts" / "pi_runtime_poc_env.sh"
    external_mapping = json.dumps(_SERVICE_MAPPING, separators=(",", ":"))

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'set -euo pipefail; export DATATAP_MCP_ENDPOINTS_JSON="$1"; source "$2"; '
                'pi_poc_normalize_datatap_mapping; test "$DATATAP_MCP_URLS" = "$1"; '
                'test "$DATATAP_MCP_ENDPOINTS_JSON" = "$1"'
            ),
            "bash",
            external_mapping,
            str(helper),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_settings_mapping_is_forwarded_to_pi_as_endpoint_json(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    captured = []
    monkeypatch.setattr(comparison.PiRpcClient, "start", lambda config: captured.append(config) or object())

    factory = comparison.build_real_pi_client_factory(settings)
    factory(cast(AgentRun, SimpleNamespace(id="run-task8b")), "test-run-token")

    assert len(captured) == 1
    assert json.loads(captured[0].environment["DATATAP_MCP_ENDPOINTS_JSON"]) == _SERVICE_MAPPING
    assert "DATATAP_MCP_URLS" not in captured[0].environment


async def test_local_preflight_failure_does_not_create_round_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    settings = _settings(monkeypatch)
    case = comparison.PocCase("brand-research-v1", "q", "2026-08-07", "report", "brand_report_v3")
    begin_calls: list[tuple[Path, str]] = []

    monkeypatch.setattr(runner, "parse_args", lambda: argparse.Namespace(case="all", runtime="pi"))
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(runner.PiPocSettingsGuard, "assert_safe", lambda _settings: None)
    monkeypatch.setattr(runner, "load_cases", lambda _path: (case,))
    monkeypatch.setattr(
        runner,
        "build_real_pi_client_factory",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("pi_poc_datatap_endpoint_mapping_required")),
    )
    monkeypatch.setattr(
        runner,
        "begin_round",
        lambda output_root, round_id: begin_calls.append((output_root, round_id)) or output_root / round_id,
    )

    with pytest.raises(RuntimeError, match="pi_poc_datatap_endpoint_mapping_required"):
        await runner.main()

    assert begin_calls == []


def test_task9_runner_has_no_current_runtime_import_or_call() -> None:
    script = (Path(__file__).parents[2] / "scripts" / "run_pi_runtime_poc.py").read_text(
        encoding="utf-8"
    )

    assert "CurrentRuntimeCaseExecutor" not in script
    assert "create_agent_runtime" not in script
    assert "refresh_approved_datatap_tools" not in script
