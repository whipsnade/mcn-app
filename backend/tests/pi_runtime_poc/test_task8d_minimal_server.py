"""Task 8D：最小 POC HTTP 服务必须自行完成全部 ORM 模型注册。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.core.config import get_settings
from app.db import models as db_models  # 测试进程的清理查询需要完整元数据。
from app.db.session import SessionFactory
from app.identity.models import User, UserChannelPermission
from app.pi_runtime_poc.auth import issue_run_token
from app.pi_runtime_poc.comparison import PocCase, PocCaseFactory

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REQUIRED_TABLES = {
    "users",
    "agent_sessions",
    "agent_runs",
    "agent_run_attempts",
    "agent_steps",
    "agent_tool_calls",
    "agent_events",
    "evidence_items",
}


def _fresh_process_env() -> dict[str, str]:
    """只为导入服务的全新解释器提供非真实占位配置。"""
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "AUTH_MODE": "mock",
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "kol_insight_pi_poc",
            "MYSQL_USER": "poc_import_probe",
            "MYSQL_PASSWORD": "poc-import-probe-password",
            "JWT_SECRET": "poc-import-probe-jwt-secret-at-least-32-chars",
            "TENCENT_PLAN_BASE_URL": "https://example.invalid/v1",
            "TENCENT_PLAN_API_KEY": "poc-import-probe-model-key",
            "TENCENT_PLAN_MODEL": "poc-import-probe-model",
            "DATATAP_MCP_TOKEN": "poc-import-probe-datatap-token",
            "PI_RUNTIME_POC_ENABLED": "true",
            "PI_RUNTIME_POC_INTERNAL_SECRET": "poc-import-probe-internal-secret",
        }
    )
    return environment


def test_minimal_server_fresh_process_registers_cross_module_foreign_key_tables() -> None:
    """删去中央模型注册时，独立服务进程不能解析 AgentRun→User 外键。"""
    assert db_models.User.__tablename__ == "users"
    probe = """
import json
import app.pi_runtime_poc.server  # noqa: F401
from app.db.base import Base

required = {
    "users", "agent_sessions", "agent_runs", "agent_run_attempts",
    "agent_steps", "agent_tool_calls", "agent_events", "evidence_items",
}
missing = sorted(required.difference(Base.metadata.tables))
print(json.dumps({"missing": missing}, sort_keys=True))
raise SystemExit(0 if not missing else 1)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_BACKEND_DIR,
        env=_fresh_process_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    observed = json.loads(result.stdout)
    assert observed == {"missing": []}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_minimal_server(port: int, environment: dict[str, str]) -> subprocess.Popen[bytes]:
    """测试拥有的本机 POC 服务生命周期入口。"""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.pi_runtime_poc.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "critical",
        ],
        cwd=_BACKEND_DIR,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _seed_smoke_run(case_id: str) -> tuple[str, str]:
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"
    factory = PocCaseFactory(
        SessionFactory,
        round_id="single-datatap-smoke",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(
        PocCase(
            case_id=case_id,
            user_question="仅验证 POC 内部审计 HTTP 路径。",
            date_anchor="2026-08-07",
            expected_behavior="refuse",
            required_artifact_type=None,
        )
    )
    async with SessionFactory() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        user_id = run.user_id
        repository = AgentRunRepository(db)
        await repository.begin_attempt(run_id)
        assert await repository.claim_lease(run_id, "pi-poc-smoke", 120)
        await db.commit()
    return run_id, user_id


async def _delete_seeded_runs(run_ids: list[str], user_ids: list[str]) -> None:
    if not run_ids:
        return
    async with SessionFactory() as db:
        runs = list((await db.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids)))).all())
        for run in runs:
            run.input_message_id = None
        await db.flush()
        await db.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
        await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
        await db.execute(delete(AgentStep).where(AgentStep.run_id.in_(run_ids)))
        await db.execute(delete(AgentMessage).where(AgentMessage.run_id.in_(run_ids)))
        await db.execute(delete(AgentRunAttempt).where(AgentRunAttempt.run_id.in_(run_ids)))
        await db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
        await db.execute(delete(AgentSession).where(AgentSession.user_id.in_(user_ids)))
        await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


async def _wait_for_health(client: httpx.AsyncClient) -> None:
    for _ in range(50):
        try:
            response = await client.get("/healthz")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("poc_minimal_server_not_ready")


@pytest.mark.skipif(
    os.environ.get("RUN_PI_POC_MYSQL_TESTS") != "1",
    reason="仅在显式隔离 kol_insight_pi_poc MySQL 验收中执行",
)
async def test_minimal_server_http_persists_diagnostic_and_zero_point_tool_call_in_poc_mysql() -> None:
    """删去中央注册时，真实 HTTP diagnostics/start 会在 ORM 外键解析前返回 500。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"
    run_ids: list[str] = []
    user_ids: list[str] = []
    server: subprocess.Popen[bytes] | None = None
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    try:
        run_id, user_id = await _seed_smoke_run(f"task8d-minimal-http-{uuid4()}")
        run_ids.append(run_id)
        user_ids.append(user_id)
        token = issue_run_token(run_id, settings=settings)
        server_env = os.environ.copy()
        server_env.update(
            {
                "APP_ENV": "test",
                "AUTH_MODE": "mock",
                "MYSQL_DATABASE": "kol_insight_pi_poc",
                "PI_RUNTIME_POC_ENABLED": "true",
            }
        )
        server = _start_minimal_server(port, server_env)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
            await _wait_for_health(client)
            diagnostic = await client.post(
                f"/api/v1/internal/pi-poc/runs/{run_id}/diagnostics",
                headers=headers,
                json={
                    "stage": "audit_start",
                    "service_slug": "social-grow-content-mcp",
                    "tool_name": "hotwords_xiaohongshu_dictionary",
                },
            )
            assert diagnostic.status_code == 200
            started = await client.post(
                f"/api/v1/internal/pi-poc/runs/{run_id}/tool-calls/start",
                headers=headers,
                json={
                    "call_id": "task8d-minimal-http-call",
                    "tool_name": "hotwords_xiaohongshu_dictionary",
                    "arguments": {},
                },
            )
            assert started.status_code == 200

            fail_run_id, fail_user_id = await _seed_smoke_run(f"task8d-smoke-failed-{uuid4()}")
            run_ids.append(fail_run_id)
            user_ids.append(fail_user_id)
            failed = await client.post(
                f"/api/v1/internal/pi-poc/runs/{fail_run_id}/smoke-failed",
                headers={"Authorization": f"Bearer {issue_run_token(fail_run_id, settings=settings)}"},
                json={"code": "pi_poc_audit_start_failed"},
            )
            assert failed.status_code == 200

        async with SessionFactory() as db:
            diagnostic_steps = int(
                (await db.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(AgentStep.run_id == run_id, AgentStep.step_type == "pi_extension_diagnostic")
                ))
                or 0
            )
            calls = list((await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))).all())
            events = list(
                (await db.scalars(
                    select(AgentEvent.event_type)
                    .where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.sequence)
                )).all()
            )
            failed_run = await db.get(AgentRun, fail_run_id)
            failed_call_count = int(
                (await db.scalar(
                    select(func.count()).select_from(AgentToolCall).where(AgentToolCall.run_id == fail_run_id)
                ))
                or 0
            )
        assert diagnostic_steps == 1
        assert len(calls) == 1
        assert (calls[0].points_reserved, calls[0].points_settled) == (0, 0)
        assert events == ["tool.started"]
        assert failed_run is not None
        assert failed_run.status == RunStatus.FAILED.value
        assert failed_call_count == 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        await _delete_seeded_runs(run_ids, user_ids)
