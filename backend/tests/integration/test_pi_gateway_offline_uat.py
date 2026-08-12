"""方案 B 完整离线进程级 UAT（fake provider，真实代码拓扑）。

拓扑：测试 MySQL + FastAPI 子进程 + 生产 Pi Gateway 可执行文件 +
fake OpenAI 兼容模型 + fake DataTap MCP（真实 Streamable HTTP）。
0 外部网络、0 真实模型、0 真实 DataTap、0 真实钱包；不触碰历史 round。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentToolCall,
    EvidenceItem,
)
from app.billing.models import TenantWallet, TenantWalletTransaction
from app.db.session import SessionFactory
from app.tenancy.models import Tenant

from .pi_uat.fake_model import (
    step_hang,
    step_internal,
    step_mcp,
    step_text,
    tool_result_texts,
)
from .pi_uat.harness import GATEWAY_SECRET, PiUatTopology, purge_uat_residue

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _uat_file_scope_cleanup():
    """清理中断轮次的 UAT 残留；harness 已硬断言只允许 test DB。"""
    await purge_uat_residue()
    try:
        yield
    finally:
        await purge_uat_residue()

BRAND = "测试品牌"
BRAND_SCOPE = {
    "brand": BRAND,
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu", "douyin"],
    "keywords": ["测试"],
    "comparison_mode": "none",
}

_GROUP_BY_SOURCE = {
    "social_statistic_overview": "overview_current",
    "social_statistic_trend": "daily_trend",
    "query_raw_posts": "top_posts",
    "query_analysis_data": "sentiment",
}


def _evidence_groups(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """从最近一次 search_evidence 结果解析 (source_name -> evidence_id) 分组。"""
    texts = tool_result_texts(messages)
    assert texts, "search_evidence 工具结果缺失"
    payload = json.loads(texts[-1])
    summary = json.loads(payload["safe_summary"])
    groups: dict[str, list[str]] = {}
    for match in summary["matches"]:
        group = _GROUP_BY_SOURCE.get(match.get("source_name"))
        evidence_id = match.get("evidence_id")
        if group and evidence_id:
            groups.setdefault(group, []).append(evidence_id)
    return groups


def _build_brand_step(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return step_internal(
        "build_brand_report_draft",
        {"scope": BRAND_SCOPE, "evidence": _evidence_groups(messages)},
    )


def _publish_step(messages: list[dict[str, Any]]) -> dict[str, Any]:
    texts = tool_result_texts(messages)
    assert texts, "build 工具结果缺失"
    # 工具结果外层是 JSON，draft_id 在转义的 safe_summary 内层 JSON 里，
    # 必须两次 json.loads（正则匹配不到 \"draft_id\" 转义形态）。
    payload = json.loads(texts[-1])
    summary = json.loads(payload["safe_summary"])
    draft_id = summary.get("draft_id")
    assert draft_id, f"未从构建结果解析到 draft_id: {texts[-1][:200]}"
    return step_internal("publish_artifacts", {"draft_ids": [draft_id]})


def _brand_script() -> list[Any]:
    return [
        step_internal("get_session_context"),
        step_mcp("insight-cube", "social_statistic_overview", {"brand": BRAND}),
        step_mcp("insight-cube", "social_statistic_trend", {"brand": BRAND}),
        step_mcp("insight-cube", "query_raw_posts", {"brand": BRAND}),
        step_mcp("insight-cube", "query_analysis_data", {"keyword": BRAND}),
        step_internal("search_evidence", {}),
        _build_brand_step,
        _publish_step,
        step_text("已完成品牌报告并发布。"),
    ]


async def _create_activate_pi_config(topology: PiUatTopology, tenant_id: str) -> str:
    """经管理 API 创建并激活一个租户 Pi 配置版本（灰度升级路径），返回 config_id。"""
    async with topology.admin_client() as admin:
        created = await admin.post(
            "/api/v1/admin/runtime-configs",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "tenant_id": tenant_id,
                "runtime_backend": "pi",
                "model": {
                    "name": "fake-pi-model",
                    "masked_origin": "fake",
                    "provider": "fake",
                },
                "datatap": {"service": "fake", "schema_digest": "sha256:" + "a" * 64},
                "limits": {"max_decisions": 50},
                "billing": {"mcp_call_points": 10},
                "secrets": {
                    "model_base_url": f"http://127.0.0.1:{topology.model_port}/v1",
                    "model_api_key": "uat-fake-model-key",
                    "datatap_token": "uat-fake-datatap-token",
                    "datatap_urls": {
                        "insight-cube": topology.mcp_urls["insight-cube-mcp"],
                        "social-grow": topology.mcp_urls["social-grow-mcp"],
                    },
                },
            },
        )
        assert created.status_code == 201, created.text
        config_id = created.json()["id"]
        activated = await admin.post(
            f"/api/v1/admin/runtime-configs/{config_id}/activate",
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert activated.status_code == 200, activated.text
    return config_id


async def _enable_pi(topology: PiUatTopology, tenant_id: str) -> str:
    """经管理 API 创建并激活租户 Pi 配置，再切 backend（真实灰度路径）。"""
    config_id = await _create_activate_pi_config(topology, tenant_id)
    async with topology.admin_client() as admin:
        switched = await admin.patch(
            f"/api/v1/admin/tenants/{tenant_id}",
            headers={"Idempotency-Key": str(uuid4())},
            json={"runtime_backend": "pi"},
        )
        assert switched.status_code == 200, switched.text
    return config_id


async def _wait_gateway_registered(topology: PiUatTopology, timeout: float = 15.0) -> None:
    """等 Gateway 完成首次签名 claim 并注册实例（灰度前置需要健康容量）。"""
    import time

    from app.pi_gateway.models import PiGatewayInstance

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionFactory() as db:
            row = await db.scalar(
                select(PiGatewayInstance).where(PiGatewayInstance.gateway_id == topology.gateway_id)
            )
            if row is not None and row.status == "active":
                return
        await asyncio.sleep(0.2)
    raise RuntimeError("gateway_registration_timeout")


async def test_brand_report_full_chain_same_version_excel_bi_and_gate() -> None:
    topology = PiUatTopology(scripts={"brand": _brand_script()})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        # 账本断言只依赖不变量：运行前后余额差恰好等于 4 次 MCP 结算（40 分），
        # 与钱包 provisioning 的来源/时点解耦。
        async with SessionFactory() as db:
            wallet_before = await db.get(TenantWallet, tenant.tenant_id)
        assert wallet_before is not None
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, f"[scenario:brand]\n请分析{BRAND}近期表现")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        terminal_status = await topology.wait_run_terminal(run.id)
        assert terminal_status in ("completed", "completed_with_warnings")

        async with SessionFactory() as db:
            # message.completed 必须先于唯一终态事件
            events = list(
                (
                    await db.scalars(
                        select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)
                    )
                ).all()
            )
            types = [event.event_type for event in events]
            assert "message.completed" in types
            assert types[-1] == "run.completed"
            assert types.index("message.completed") < types.index("run.completed")
            # run.started 每次会话恰好一次（agent_start），终态事件恰好一次
            assert types.count("run.started") == 1
            assert len([t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]) == 1
            messages = list(
                (
                    await db.scalars(
                        select(AgentMessage).where(AgentMessage.run_id == run.id, AgentMessage.role == "assistant")
                    )
                ).all()
            )
            assert len(messages) == 1
            assert "品牌报告" in messages[0].content
            # MCP 调用：4 次真实外发全部 settled，租户账本恰好结算 40 分
            calls = list(
                (await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))).all()
            )
            assert len(calls) == 4
            assert all(call.status == "settled" for call in calls)
            wallet = await db.get(TenantWallet, tenant.tenant_id)
            assert wallet is not None
            # 账务硬绑定：钱包净支出必须恰好等于 fake MCP 实际收到的调用数 ×10。
            mcp_received = len(topology.mcp_services[0].calls)
            assert mcp_received == 4
            assert (wallet.balance, wallet.reserved) == (
                wallet_before.balance - mcp_received * 10,
                0,
            )
            ledger = list(
                (
                    await db.scalars(
                        select(TenantWalletTransaction).where(
                            TenantWalletTransaction.tenant_id == tenant.tenant_id,
                            TenantWalletTransaction.kind == "settle",
                        )
                    )
                ).all()
            )
            assert len(ledger) == 4
            # 正式产物：不可变 Version、同版本 Excel/BI
            versions = list(
                (
                    await db.scalars(
                        select(AgentArtifactVersion)
                        .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                        .where(AgentArtifact.session_id == session_id)
                    )
                ).all()
            )
            assert len(versions) == 1
            version = versions[0]
            assert version.schema_version == "brand_report_v3"
            payload = version.payload_json
            assert payload["data"]["overview"]["total_volume"] == 320
            assert payload["data_status"] in ("complete", "restricted")

        # Excel 与 BI 绑定同一 Version
        async with topology.client_for(user) as client:
            export = await client.get(f"/api/v1/agent/artifacts/{version.artifact_id}/export")
            assert export.status_code == 200
            assert export.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument"
            )
            detail = await client.get(
                f"/api/v1/agent/artifacts/{version.artifact_id}/versions/{version.version}"
            )
            assert detail.status_code == 200
            assert detail.json()["version"] == version.version

        # B0 发布门禁对正式产物复核：结构化 claims 与 lineage 必须仍成立
        from app.agent_artifacts.lineage import validate_structured_claims

        async with SessionFactory() as db:
            evidence_rows = list(
                (
                    await db.scalars(
                        select(EvidenceItem).where(EvidenceItem.session_id == session_id)
                    )
                ).all()
            )
            issues = validate_structured_claims(
                version.payload_json,
                version.id,
                {
                    "user_id": user.user_id,
                    "session_id": session_id,
                    "run_id": run.id,
                    "evidence": {
                        row.id: {
                            "user_id": user.user_id,
                            "session_id": session_id,
                            "run_id": row.run_id,
                            "source_type": row.source_type,
                        }
                        for row in evidence_rows
                    },
                },
            )
            assert issues == []

        # fake MCP 收到的恰好是 4 次已审核工具的调用
        insight = topology.mcp_services[0]
        assert len(insight.calls) == 4


# --------------------------------------------------------------------------- #
# 批次 A：边界与护栏场景（复用同一离线拓扑）
# --------------------------------------------------------------------------- #


async def _session_artifact_versions(session_id: str) -> list[AgentArtifactVersion]:
    """该 session 已发布的全部 Artifact Version（新会话，REPEATABLE READ 安全）。"""
    async with SessionFactory() as db:
        return list(
            (
                await db.scalars(
                    select(AgentArtifactVersion)
                    .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                    .where(AgentArtifact.session_id == session_id)
                )
            ).all()
        )


async def _run_tool_calls(run_id: str) -> list[AgentToolCall]:
    async with SessionFactory() as db:
        return list(
            (await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))).all()
        )


async def test_clarification_zero_artifact_zero_mcp() -> None:
    """request_clarification 直接收口：0 外发、0 产物、澄清消息落库。"""
    topology = PiUatTopology(
        scripts={
            "clarify": [
                step_internal(
                    "request_clarification",
                    {"question": "请确认本次分析的目标平台", "options": ["小红书", "抖音"]},
                ),
                step_text("请补充信息"),
            ]
        }
    )
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, "[scenario:clarify]\n帮我做个分析")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        terminal_status = await topology.wait_run_terminal(run.id)
        # 工具把 Run 迁移到 clarification_requested；gateway 后续只可能保持或
        # 以 completed 系收口，绝不允许 failed/cancelled 之外的模糊状态。
        assert terminal_status in (
            "clarification_requested",
            "completed",
            "completed_with_warnings",
        )
        # 硬断言：0 次真实 MCP 外发（两个 fake 服务均无调用）
        assert topology.mcp_services[0].calls == []
        assert topology.mcp_services[1].calls == []
        # 硬断言：0 个正式产物 Version
        assert await _session_artifact_versions(session_id) == []
        # 澄清问题以 assistant 消息落库（request_clarification 工具直写）
        async with SessionFactory() as db:
            messages = list(
                (
                    await db.scalars(
                        select(AgentMessage).where(
                            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
                        )
                    )
                ).all()
            )
        assert len(messages) >= 1
        assert any("请确认本次分析的目标平台" in m.content for m in messages)


async def test_non_marketing_refusal_zero_side_effects() -> None:
    """非营销问题纯文本拒答：0 工具调用、0 外发、0 产物。"""
    refusal = "该问题超出营销分析范围。"
    topology = PiUatTopology(scripts={"refuse": [step_text(refusal)]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, "[scenario:refuse]\n帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        terminal_status = await topology.wait_run_terminal(run.id)
        assert terminal_status in ("completed", "completed_with_warnings")
        # 硬断言：0 个 AgentToolCall、0 次真实外发、0 个产物 Version
        assert await _run_tool_calls(run.id) == []
        assert topology.mcp_services[0].calls == []
        assert topology.mcp_services[1].calls == []
        assert await _session_artifact_versions(session_id) == []
        async with SessionFactory() as db:
            messages = list(
                (
                    await db.scalars(
                        select(AgentMessage).where(
                            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
                        )
                    )
                ).all()
            )
        assert len(messages) == 1
        assert refusal in messages[0].content


async def test_insufficient_balance_blocks_mcp_with_zero_external_calls() -> None:
    """余额 < 10 时 preflight 拒绝：0 次真实外发、钱包不变、无预留/结算流水。"""
    # 不用完整 _brand_script()：其余额为 0 证据时动态 build/publish 步骤会走
    # 异常分支，与本次要验证的计费门禁无关；短脚本聚焦「第一次 MCP 即被拒」。
    topology = PiUatTopology(
        scripts={
            "brand": [
                step_internal("get_session_context"),
                step_mcp("insight-cube", "social_statistic_overview", {"brand": BRAND}),
                step_text("积分不足，已停止外呼。"),
            ]
        }
    )
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        # 把租户钱包压到 5（< 一次 MCP 调用的 10 积分）；钱包在登录时已完成
        # welcome 置备，这里直接改库等价于管理员调账。
        async with SessionFactory.begin() as db:
            await db.execute(
                update(TenantWallet)
                .where(TenantWallet.tenant_id == tenant.tenant_id)
                .values(balance=5, reserved=0)
            )
        async with SessionFactory() as db:
            wallet = await db.get(TenantWallet, tenant.tenant_id)
        assert wallet is not None
        assert (wallet.balance, wallet.reserved) == (5, 0)
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, f"[scenario:brand]\n请分析{BRAND}")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        # 终态不设硬断言：preflight 被拒后 gateway/脚本如何收口是实现细节；
        # 硬断言是「0 外发 + 账不变」。
        terminal_status = await topology.wait_run_terminal(run.id)
        assert terminal_status in ("completed", "completed_with_warnings", "failed")
        assert topology.mcp_services[0].calls == []
        assert topology.mcp_services[1].calls == []
        async with SessionFactory() as db:
            wallet = await db.get(TenantWallet, tenant.tenant_id)
            assert wallet is not None
            assert (wallet.balance, wallet.reserved) == (5, 0)
            ledger_count = await db.scalar(
                select(func.count())
                .select_from(TenantWalletTransaction)
                .where(
                    TenantWalletTransaction.tenant_id == tenant.tenant_id,
                    TenantWalletTransaction.kind.in_(("reserve", "settle", "release", "unknown")),
                )
            )
        assert ledger_count == 0


async def test_unreachable_mcp_releases_reservation_with_zero_external_calls() -> None:
    """MCP 服务不可达（本地未外发错误）：definitely_not_sent 释放预留。

    硬断言：fake MCP 实际收到 0 次调用，钱包净支出 == 实际外发数 ×10 == 0。
    覆盖 adapter 本地错误（not_connected/init_failed/server_backoff 等）不得
    进入成功结算分支的进程级证据。
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = int(sock.getsockname()[1])
    topology = PiUatTopology(
        scripts={
            "deadmcp": [
                step_internal("get_session_context"),
                step_mcp("insight-cube", "social_statistic_overview", {"brand": BRAND}),
                step_text("数据服务暂不可用。"),
            ]
        }
    )
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        # 自定义配置：datatap_urls 指向已关闭的 loopback 端口
        async with topology.admin_client() as admin:
            created = await admin.post(
                "/api/v1/admin/runtime-configs",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "tenant_id": tenant.tenant_id,
                    "runtime_backend": "pi",
                    "model": {"name": "fake-pi-model", "masked_origin": "fake", "provider": "fake"},
                    "datatap": {"service": "fake", "schema_digest": "sha256:" + "a" * 64},
                    "limits": {"max_decisions": 50},
                    "billing": {"mcp_call_points": 10},
                    "secrets": {
                        "model_base_url": f"http://127.0.0.1:{topology.model_port}/v1",
                        "model_api_key": "uat-fake-model-key",
                        "datatap_token": "uat-fake-datatap-token",
                        "datatap_urls": {
                            "insight-cube": f"http://127.0.0.1:{dead_port}/api/gateway/insight-cube-mcp/mcp",
                            "social-grow": f"http://127.0.0.1:{dead_port}/api/gateway/social-grow-mcp/mcp",
                        },
                    },
                },
            )
            assert created.status_code == 201, created.text
            config_id = created.json()["id"]
            activated = await admin.post(
                f"/api/v1/admin/runtime-configs/{config_id}/activate",
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert activated.status_code == 200, activated.text
            switched = await admin.patch(
                f"/api/v1/admin/tenants/{tenant.tenant_id}",
                headers={"Idempotency-Key": str(uuid4())},
                json={"runtime_backend": "pi"},
            )
            assert switched.status_code == 200, switched.text
        user = tenant.users[0]
        async with SessionFactory() as db:
            wallet_before = await db.get(TenantWallet, tenant.tenant_id)
        assert wallet_before is not None
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, f"[scenario:deadmcp]\n请分析{BRAND}")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        terminal_status = await topology.wait_run_terminal(run.id)
        # 终态不设硬断言（模型收到工具错误后的收口路径是实现细节）。
        assert terminal_status in ("completed", "completed_with_warnings", "failed")
        mcp_received = len(topology.mcp_services[0].calls) + len(topology.mcp_services[1].calls)
        assert mcp_received == 0
        async with SessionFactory() as db:
            wallet = await db.get(TenantWallet, tenant.tenant_id)
            assert wallet is not None
            # 钱包净支出 == 实际外发数 ×10 == 0；预留全部释放
            assert (wallet.balance, wallet.reserved) == (
                wallet_before.balance - mcp_received * 10,
                0,
            )
            settled = await db.scalar(
                select(func.count())
                .select_from(TenantWalletTransaction)
                .where(
                    TenantWalletTransaction.tenant_id == tenant.tenant_id,
                    TenantWalletTransaction.kind == "settle",
                )
            )
        assert settled == 0


async def test_session_mutex_rejects_concurrent_message() -> None:
    """同 Session 有活动 Run 时第二条消息 409；取消首条后拓扑干净收尾。"""
    topology = PiUatTopology(scripts={"slow": [step_hang(30), step_text("完成")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        first = await topology.send_message(user, session_id, "[scenario:slow]\n慢慢分析")
        assert first.status_code in (200, 201, 202), first.text
        run = await topology.run_by_session(session_id)
        # 第一条 POST 返回即已提交 queued Run（active 状态），第二条必被互斥
        second = await topology.send_message(user, session_id, "[scenario:slow]\n再来一条")
        assert second.status_code == 409, second.text
        assert second.json()["detail"] == "active_run_in_progress"
        # 取消第一条（running 走 request_cancel，由 gateway 心跳收口；hang 30s
        # 是上限兜底），避免 teardown 时还有悬挂 Run
        async with topology.client_for(user) as client:
            cancel = await client.post(f"/api/v1/agent/runs/{run.id}/cancel")
        assert cancel.status_code == 200, cancel.text
        terminal_status = await topology.wait_run_terminal(run.id, timeout=120)
        assert terminal_status == "cancelled"


async def test_cross_tenant_isolation() -> None:
    """租户 B 用户访问租户 A 的 session/run/events 一律 404/403，DB 层互不可见。"""
    topology = PiUatTopology(scripts={"refuse": [step_text("该问题超出营销分析范围。")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant_a = topology.tenants["uat-tenant-a"]
        tenant_b = topology.tenants["uat-tenant-b"]
        await _enable_pi(topology, tenant_a.tenant_id)
        user_a = tenant_a.users[0]
        session_a = await topology.create_session(user_a)
        response = await topology.send_message(user_a, session_a, "[scenario:refuse]\n帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_a)
        terminal_status = await topology.wait_run_terminal(run.id)
        assert terminal_status in ("completed", "completed_with_warnings")
        # 对照组：A 自己能读到，证明资源确实存在
        async with topology.client_for(user_a) as client_a:
            own = await client_a.get(f"/api/v1/agent/sessions/{session_a}")
            assert own.status_code == 200
        # B 用户访问 A 的资源：全部 404/403，不得 200、不得泄漏存在性
        user_b = tenant_b.users[0]
        async with topology.client_for(user_b) as client_b:
            for path in (
                f"/api/v1/agent/sessions/{session_a}",
                f"/api/v1/agent/runs/{run.id}",
                f"/api/v1/agent/runs/{run.id}/events",
            ):
                denied = await client_b.get(path)
                assert denied.status_code in (403, 404), (path, denied.status_code)
        # DB 层物理隔离：按 B 租户条件查询，A 的 session/run 不可见
        async with SessionFactory() as db:
            b_sessions = list(
                (
                    await db.scalars(
                        select(AgentSession.id).where(AgentSession.tenant_id == tenant_b.tenant_id)
                    )
                ).all()
            )
            b_runs = list(
                (
                    await db.scalars(
                        select(AgentRun.id).where(AgentRun.tenant_id == tenant_b.tenant_id)
                    )
                ).all()
            )
        assert session_a not in b_sessions
        assert run.id not in b_runs


def _gateway_signature(method: str, path: str, timestamp: int, nonce: str, body: bytes) -> str:
    """与 pi-gateway buildSignature 完全一致的签名：sha256(body) hex 进签名串。"""
    body_hash = hashlib.sha256(body).hexdigest()
    signing = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()
    return hmac.new(GATEWAY_SECRET.encode("utf-8"), signing, hashlib.sha256).hexdigest()


async def test_nonce_replay_rejected() -> None:
    """内部端点 HMAC 门禁：同 nonce 重放 401；签名不变篡改 body 也 401。"""
    topology = PiUatTopology()
    async with topology:
        # 被测对象是 FastAPI 的签名认证与 nonce 屏障，不需要 gateway 参与；
        # 先停掉它，避免其 100ms claim 循环与手工请求在 nonce 屏障表上并发
        # 竞争（偶发锁等待会让状态码偏离被测语义）。
        await topology.stop_gateway()
        path = "/api/v1/internal/pi-gateway/v1/claims"
        body = json.dumps({"capacity": 1}).encode()
        timestamp = int(time.time())
        nonce = uuid4().hex
        headers = {
            "Content-Type": "application/json",
            "X-Pi-Gateway-Id": topology.gateway_id,
            "X-Pi-Timestamp": str(timestamp),
            "X-Pi-Nonce": nonce,
            "X-Pi-Signature": _gateway_signature("POST", path, timestamp, nonce, body),
        }
        async with httpx.AsyncClient(base_url=topology.api_base, timeout=10) as client:
            # 第一次：签名合法 → 通过认证（无待 claim 的 Run 时 204）
            first = await client.post(path, content=body, headers=headers)
            assert first.status_code in (200, 204), first.text
            # 第二次：逐字节重放同一请求 → nonce 屏障唯一约束命中，401
            replay = await client.post(path, content=body, headers=headers)
            assert replay.status_code == 401
            assert replay.json()["detail"] == "pi_gateway_auth_failed"
            # 篡改：换新 nonce 但签名仍按原 body 计算 → 签名校验失败，401
            tampered_body = json.dumps({"capacity": 2}).encode()
            tampered = await client.post(
                path,
                content=tampered_body,
                headers={**headers, "X-Pi-Nonce": uuid4().hex},
            )
            assert tampered.status_code == 401
            assert tampered.json()["detail"] == "pi_gateway_auth_failed"


# --------------------------------------------------------------------------- #
# 批次 B：钻取版本绑定 / License 中途暂停 / 取消收口 / worker 崩溃恢复
# --------------------------------------------------------------------------- #


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """取消息历史中最后一条 user 文本（动态步骤从中解析测试嵌入的真实 id）。"""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if isinstance(content, str):
            return content
    return ""


def _message_tag(messages: list[dict[str, Any]], tag: str) -> str:
    """从用户消息文本解析 ``tag=value``（测试把真实 artifact/version id 拼进消息）。"""
    match = re.search(rf"{tag}=([0-9a-zA-Z-]+)", _last_user_text(messages))
    assert match, f"用户消息缺少 {tag}= 标记: {_last_user_text(messages)[:200]}"
    return match.group(1)


def _drilldown_step(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """钻取脚本的状态驱动动态步骤：按已出现的工具结果决定下一步。

    同一 Session 第二条 Run 的模型上下文带着第一条 Run 的 assistant 完成
    消息，按 assistant 轮数推进的脚本下标会整体偏移；因此不按固定下标
    编排，而是看工具结果进度决策（read → build → publish → finish）。
    """
    texts = tool_result_texts(messages)
    if not texts:
        # 第一步：读取父 Version（真实 id 由测试拼进用户消息文本）
        return step_internal(
            "read_artifact",
            {
                "artifact_id": _message_tag(messages, "artifact_id"),
                "version": int(_message_tag(messages, "version")),
            },
        )
    # 工具结果外层是 JSON，safe_summary 是转义的内层 JSON 字符串（两次 loads）
    summary = json.loads(json.loads(texts[-1])["safe_summary"])
    if isinstance(summary, dict) and "payload" in summary:
        # read_artifact 已完成：确认读到的就是要钻取的 Artifact，绑定该
        # Version 行 id 构建钻取看板（数值只允许 value_ref 引用，不填字面值）
        assert summary["artifact_id"] == _message_tag(messages, "artifact_id")
        version_id = _message_tag(messages, "version_id")
        return step_internal(
            "build_insight_draft",
            {
                "parent_artifact_version_id": version_id,
                "question": "本期总声量是多少？",
                "title": "总声量钻取",
                "blocks": [
                    {
                        "type": "metric_grid",
                        "title": "核心指标",
                        "cards": [
                            {
                                "key": "total_volume",
                                "label": "总声量",
                                "value_ref": {
                                    "source_type": "artifact",
                                    "artifact_version_id": version_id,
                                    "source_path": "/data/overview/total_volume",
                                },
                                "unit": "条",
                            }
                        ],
                    }
                ],
            },
        )
    if isinstance(summary, dict) and summary.get("draft_id"):
        # build_insight_draft 已完成：发布刚落库的 Draft
        return step_internal("publish_artifacts", {"draft_ids": [summary["draft_id"]]})
    # publish_artifacts 的 safe_summary 是 list：发布完成，收尾
    return step_text("钻取完成")


async def test_drilldown_binds_exact_version_with_zero_datatap() -> None:
    """钻取看板行级绑定父 Version：第二条 Run 0 次 DataTap 外发、0 个 MCP 调用。"""
    topology = PiUatTopology(
        scripts={"brand": _brand_script(), "drilldown": [_drilldown_step] * 8}
    )
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        # 第一条 Run：完整品牌链路，发布正式 Version 作为钻取父级
        first = await topology.send_message(
            user, session_id, f"[scenario:brand]\n请分析{BRAND}近期表现"
        )
        assert first.status_code in (200, 201, 202), first.text
        run1 = await topology.run_by_session(session_id)
        assert await topology.wait_run_terminal(run1.id) in (
            "completed",
            "completed_with_warnings",
        )
        versions = await _session_artifact_versions(session_id)
        assert len(versions) == 1
        brand_version = versions[0]
        assert brand_version.schema_version == "brand_report_v3"
        mcp_before = [len(service.calls) for service in topology.mcp_services]
        # 第二条 Run：同一 Session 钻取，真实 artifact/version id 拼进消息文本
        second = await topology.send_message(
            user,
            session_id,
            "[scenario:drilldown]\n"
            f"请钻取 artifact_id={brand_version.artifact_id} "
            f"version={brand_version.version} version_id={brand_version.id} 的总声量",
        )
        assert second.status_code in (200, 201, 202), second.text
        run2 = await topology.run_by_session(session_id)
        assert run2.id != run1.id
        assert await topology.wait_run_terminal(run2.id) in (
            "completed",
            "completed_with_warnings",
        )
        # 硬断言：第二条 Run 期间 0 次 DataTap 外发（两个 fake 服务 calls 不增长）
        assert [len(service.calls) for service in topology.mcp_services] == mcp_before
        # 硬断言：第二条 Run 没有任何 MCP AgentToolCall（钻取全部走内部工具）
        assert await _run_tool_calls(run2.id) == []
        # 硬断言：新 insight 产物绑定父 Artifact 与父 Version 行 id
        async with SessionFactory() as db:
            artifacts = list(
                (
                    await db.scalars(
                        select(AgentArtifact).where(AgentArtifact.session_id == session_id)
                    )
                ).all()
            )
        by_module = {artifact.module: artifact for artifact in artifacts}
        insight = by_module.get("insight")
        assert insight is not None
        assert insight.parent_artifact_id == brand_version.artifact_id
        versions_after = await _session_artifact_versions(session_id)
        assert len(versions_after) == 2
        insight_version = next(v for v in versions_after if v.artifact_id == insight.id)
        assert insight_version.schema_version == "insight_board_v1"
        assert insight_version.parent_artifact_version_id == brand_version.id
        # 钻取数值来自父 Version 的真实字段（fake fixture 总声量 320）
        card = insight_version.payload_json["data"][0]["cards"][0]
        assert card["value"] == 320


async def test_license_suspended_mid_run_blocks_further_mcp() -> None:
    """Run 中途暂停租户 License：后续 MCP preflight 被拒，0 新增外发、只结 10 分。"""
    tenant_holder: dict[str, str] = {}

    async def _suspend_then_mcp(messages: list[dict[str, Any]]) -> dict[str, Any]:
        # 动态步骤在 pytest 进程内求值：直接改库等价于管理员暂停 License
        # （授权判定读租户级 license_status，TenantLicense 表本身无状态列）。
        del messages
        async with SessionFactory.begin() as db:
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_holder["tenant_id"])
                .values(license_status="suspended")
            )
        return step_mcp("insight-cube", "social_statistic_trend", {"brand": BRAND})

    topology = PiUatTopology(
        scripts={
            "license": [
                step_mcp("insight-cube", "social_statistic_overview", {"brand": BRAND}),
                _suspend_then_mcp,
                step_text("已停止"),
            ]
        }
    )
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        tenant_holder["tenant_id"] = tenant.tenant_id
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        # 账本断言只依赖不变量：运行前后差恰好等于 1 次 MCP 结算（10 分）
        async with SessionFactory() as db:
            wallet_before = await db.get(TenantWallet, tenant.tenant_id)
        assert wallet_before is not None
        session_id = await topology.create_session(user)
        response = await topology.send_message(
            user, session_id, f"[scenario:license]\n请分析{BRAND}"
        )
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        # 终态不设硬断言（preflight 被拒后如何收口是实现细节）；
        # 硬断言是「恰好 1 次外发 + 只结 10 分」。
        terminal_status = await topology.wait_run_terminal(run.id)
        assert terminal_status in ("completed", "completed_with_warnings", "failed")
        # 硬断言：恰好 1 次真实外发（License 暂停后第二次 preflight 被拒，0 新增）
        assert len(topology.mcp_services[0].calls) == 1
        assert topology.mcp_services[1].calls == []
        # 第二次 MCP 未产生任何预留/结算流水；只有第一次的 reserve+settle 各一条
        async with SessionFactory() as db:
            wallet = await db.get(TenantWallet, tenant.tenant_id)
            assert wallet is not None
            assert (wallet.balance, wallet.reserved) == (wallet_before.balance - 10, 0)
            ledger = list(
                (
                    await db.scalars(
                        select(TenantWalletTransaction).where(
                            TenantWalletTransaction.tenant_id == tenant.tenant_id,
                            TenantWalletTransaction.kind.in_(
                                ("reserve", "settle", "release", "unknown")
                            ),
                        )
                    )
                ).all()
            )
        kinds = sorted(row.kind for row in ledger)
        assert kinds == ["reserve", "settle"]
        # 第二次 MCP 的 preflight 在 License 复核处被拒（service.preflight_mcp
        # 的 feature 门禁先于 AgentToolCall 落库），因此只有第一次的 settled 行
        calls = await _run_tool_calls(run.id)
        assert len(calls) == 1
        assert calls[0].status == "settled"
        assert calls[0].internal_tool_name == "social_statistic_overview"


async def test_cancel_run_reaches_cancelled_terminal() -> None:
    """running 中的 Run 取消：恰好一个 run.cancelled、无 run.completed、不翻转。"""
    topology = PiUatTopology(scripts={"slow": [step_hang(60), step_text("完成")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, "[scenario:slow]\n慢慢分析")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)
        # 等 gateway claim 进入 running：覆盖「在飞执行只写 cancel_requested、
        # 由心跳收口」的取消路径（queued 立即取消是另一条路径）
        deadline = time.monotonic() + 30
        while True:
            async with SessionFactory() as db:
                current = await db.get(AgentRun, run.id)
            if current is not None and current.status == "running":
                break
            assert time.monotonic() < deadline, "run_not_running_timeout"
            await asyncio.sleep(0.2)
        async with topology.client_for(user) as client:
            cancel = await client.post(f"/api/v1/agent/runs/{run.id}/cancel")
        assert cancel.status_code == 200, cancel.text
        # hang 60s 是上限兜底：心跳发现 cancel_requested 后 abort 子进程，
        # Run 应在远小于 60s 内收口 cancelled
        terminal_status = await topology.wait_run_terminal(run.id, timeout=120)
        assert terminal_status == "cancelled"
        async with SessionFactory() as db:
            events = list(
                (
                    await db.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == run.id)
                        .order_by(AgentEvent.sequence)
                    )
                ).all()
            )
        types = [event.event_type for event in events]
        # 硬断言：终态事件唯一且为 run.cancelled；不存在 run.completed；
        # 模型始终 hang，不存在 message.completed
        assert types.count("run.cancelled") == 1
        assert "run.completed" not in types
        assert "run.failed" not in types
        assert "message.completed" not in types
        assert len([t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]) == 1
        # 终态不翻转：短暂等待后状态与事件数保持稳定（gateway 不会再发 terminal）
        await asyncio.sleep(2)
        async with SessionFactory() as db:
            after = await db.get(AgentRun, run.id)
            event_count = await db.scalar(
                select(func.count()).select_from(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        assert after is not None and after.status == "cancelled"
        assert event_count == len(events)


async def _worker_pids(topology: PiUatTopology) -> set[int]:
    """读取本拓扑 Gateway 自己登记的 worker 子进程 PID。"""
    if topology.gateway_pgid is None:
        return set()
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"http://127.0.0.1:{topology.gateway_health_port}/metrics")
    response.raise_for_status()
    payload = response.json()
    raw_pids = payload.get("worker_pids", [])
    if not isinstance(raw_pids, list):
        raise AssertionError("gateway_worker_pids_invalid")
    return {pid for pid in raw_pids if isinstance(pid, int) and pid > 0}


async def _run_attempts(run_id: str) -> list[AgentRunAttempt]:
    async with SessionFactory() as db:
        return list(
            (
                await db.scalars(
                    select(AgentRunAttempt)
                    .where(AgentRunAttempt.run_id == run_id)
                    .order_by(AgentRunAttempt.attempt)
                )
            ).all()
        )


async def test_worker_crash_single_recovery_then_failed_on_second_crash() -> None:
    """worker SIGKILL：第一次基础设施丢失恢复一次新 Attempt，第二次终态 failed。"""
    model_calls = {"count": 0}

    def _hang_step(messages: list[dict[str, Any]]) -> dict[str, Any]:
        # 每次模型调用都 hang：配合手工 SIGKILL 制造基础设施失败；计数器
        # 同时证明恢复恰好重放了一次（无第三次 Attempt 的模型调用）。
        del messages
        model_calls["count"] += 1
        return step_hang(120)

    topology = PiUatTopology(scripts={"crash": [_hang_step]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        # 记录发消息前的 worker pid 集合做差集，排除其他测试残留进程
        baseline_pids = await _worker_pids(topology)
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, "[scenario:crash]\n开始分析")
        assert response.status_code in (200, 201, 202), response.text
        run = await topology.run_by_session(session_id)

        async def wait_new_worker(exclude: set[int], timeout: float = 180.0) -> set[int]:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = await _worker_pids(topology)
                current -= exclude
                if current:
                    return current
                await asyncio.sleep(0.2)
            raise RuntimeError("worker_spawn_timeout")

        async def wait_model_calls(expected: int, timeout: float = 180.0) -> None:
            """等该 Attempt 的模型请求真正发出（worker 进入 hang 状态）再 kill。"""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if model_calls["count"] >= expected:
                    return
                await asyncio.sleep(0.2)
            raise RuntimeError(f"model_call_timeout:{expected}")

        # Attempt 1：等 worker spawn 且模型请求 hang 住，再 SIGKILL 模拟进程崩溃
        first_pids = await wait_new_worker(baseline_pids)
        await wait_model_calls(1)
        for pid in first_pids:
            os.kill(pid, signal.SIGKILL)
        # 恢复：心跳停止 → 租约过期（5s）→ 恢复循环（1s）requeue →
        # gateway 重新 claim 落 Attempt 2 并 spawn 新 worker
        deadline = time.monotonic() + 90
        while True:
            attempts = await _run_attempts(run.id)
            if len(attempts) >= 2:
                break
            assert time.monotonic() < deadline, "second_attempt_timeout"
            await asyncio.sleep(0.5)
        second_pids = await wait_new_worker(baseline_pids | first_pids)
        await wait_model_calls(2)
        for pid in second_pids:
            os.kill(pid, signal.SIGKILL)
        # 第二次基础设施失败：终态 failed，不再有第三次 Attempt
        terminal_status = await topology.wait_run_terminal(run.id, timeout=120)
        assert terminal_status == "failed"
        # 硬断言：恰好 2 个 Attempt、模型恰好被调用 2 次（不无限重试）
        attempts = await _run_attempts(run.id)
        assert len(attempts) == 2
        assert [attempt.attempt for attempt in attempts] == [1, 2]
        assert model_calls["count"] == 2
        # 硬断言：无 run.completed，终态事件唯一（run.failed 恰好一次）
        async with SessionFactory() as db:
            events = list(
                (
                    await db.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == run.id)
                        .order_by(AgentEvent.sequence)
                    )
                ).all()
            )
            final_run = await db.get(AgentRun, run.id)
        types = [event.event_type for event in events]
        assert "run.completed" not in types
        assert "run.cancelled" not in types
        assert types.count("run.failed") == 1
        assert len([t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]) == 1
        assert final_run is not None and final_run.status == "failed"


# --------------------------------------------------------------------------- #
# 批次 C：公平调度 / draining / 灰度回切与 kill switch / SSE 续传 / 快照不可变
# --------------------------------------------------------------------------- #


async def _set_gateway_mode(topology: PiUatTopology, mode: str) -> None:
    """经管理 API 切换 gateway mode（active/draining），走真实审计与幂等路径。"""
    async with topology.admin_client() as admin:
        response = await admin.patch(
            f"/api/v1/admin/pi-runtime/gateways/{topology.gateway_id}",
            headers={"Idempotency-Key": str(uuid4())},
            json={"mode": mode},
        )
        assert response.status_code == 200, response.text


async def _switch_runtime_backend(topology: PiUatTopology, tenant_id: str, backend: str) -> None:
    """经管理 API 切换租户 runtime_backend。

    灰度前置校验（`_produce_update_tenant`）只针对「切 pi」方向；切 current
    无前置条件，admin PATCH 直接生效。
    """
    async with topology.admin_client() as admin:
        response = await admin.patch(
            f"/api/v1/admin/tenants/{tenant_id}",
            headers={"Idempotency-Key": str(uuid4())},
            json={"runtime_backend": backend},
        )
        assert response.status_code == 200, response.text


async def _wait_run_status(run_id: str, status: str, timeout: float = 30.0) -> None:
    """轮询直到 Run 进入指定状态（每轮新会话，避开 REPEATABLE READ 快照）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionFactory() as db:
            run = await db.get(AgentRun, run_id)
            if run is not None and run.status == status:
                return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"run_status_timeout:{run_id}:{status}")


def _canonical_snapshot(run: AgentRun) -> str:
    """Run 快照的规范序列化（逐字节可比）。"""
    return json.dumps(run.runtime_config_snapshot_json, sort_keys=True, ensure_ascii=False)


def _message_run_id(response: httpx.Response) -> str:
    """取 messages 响应里的用户 Run id。

    不用 ``run_by_session``（created_at 倒序首条）：会话首条消息会触发
    best-effort 的 session_title utility 内部 Run，其 created_at 更晚，按时间
    倒序可能抢到 utility Run（fake 模型不满足 utility 输出 Schema，恒
    utility_failed），造成不确定断言。响应里的 run_id 是确定的用户 Run。
    """
    run_id = response.json().get("run_id")
    assert run_id, f"messages 响应缺少 run_id: {response.text[:200]}"
    return str(run_id)


async def _debug_dump_run(run_id: str, label: str) -> None:
    """临时诊断：打印 Run 的失败细节（error_code / 事件 / Step 输出）。"""
    from app.agent_runtime.models import AgentStep

    async with SessionFactory() as db:
        run = await db.get(AgentRun, run_id)
        events = list(
            (
                await db.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.sequence)
                )
            ).all()
        )
        steps = list(
            (
                await db.scalars(
                    select(AgentStep)
                    .where(AgentStep.run_id == run_id)
                    .order_by(AgentStep.sequence)
                )
            ).all()
        )
    print(f"DEBUG {label}:", None if run is None else (run.status, run.error_code, run.runtime_backend))
    for event in events:
        print("  event", event.sequence, event.event_type, str(event.payload_json)[:300])
    for step in steps:
        print("  step", step.sequence, step.step_type, step.status, str(step.output_json)[:300])


async def test_fair_scheduling_two_tenants_share_capacity() -> None:
    """capacity=2 下两租户各一条 brand Run 并发：窗口重叠、各结 40 分、共 8 次外发。"""
    topology = PiUatTopology(scripts={"brand": _brand_script()})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant_a = topology.tenants["uat-tenant-a"]
        tenant_b = topology.tenants["uat-tenant-b"]
        # 租户 B 同样需要完整灰度路径（创建并激活自己的 runtime config 再切 pi）
        await _enable_pi(topology, tenant_a.tenant_id)
        await _enable_pi(topology, tenant_b.tenant_id)
        # 账本断言只依赖不变量：运行前后余额差恰好等于各自 4 次 MCP 结算（40 分）
        async with SessionFactory() as db:
            wallet_a_before = await db.get(TenantWallet, tenant_a.tenant_id)
            wallet_b_before = await db.get(TenantWallet, tenant_b.tenant_id)
        assert wallet_a_before is not None and wallet_b_before is not None
        # 两租户各一个 session（无会话互斥），两条消息几乎同时入队
        session_a = await topology.create_session(tenant_a.users[0])
        session_b = await topology.create_session(tenant_b.users[0])
        response_a = await topology.send_message(
            tenant_a.users[0], session_a, f"[scenario:brand]\n请分析{BRAND}近期表现"
        )
        response_b = await topology.send_message(
            tenant_b.users[0], session_b, f"[scenario:brand]\n请分析{BRAND}近期表现"
        )
        assert response_a.status_code in (200, 201, 202), response_a.text
        assert response_b.status_code in (200, 201, 202), response_b.text
        run_a_id = _message_run_id(response_a)
        run_b_id = _message_run_id(response_b)
        status_a, status_b = await asyncio.gather(
            topology.wait_run_terminal(run_a_id),
            topology.wait_run_terminal(run_b_id),
        )
        assert status_a in ("completed", "completed_with_warnings")
        assert status_b in ("completed", "completed_with_warnings")
        async with SessionFactory() as db:
            attempts_a = list(
                (
                    await db.scalars(
                        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_a_id)
                    )
                ).all()
            )
            attempts_b = list(
                (
                    await db.scalars(
                        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_b_id)
                    )
                ).all()
            )
            wallet_a = await db.get(TenantWallet, tenant_a.tenant_id)
            wallet_b = await db.get(TenantWallet, tenant_b.tenant_id)
        # 硬断言：两条 Run 各恰好 1 个 Attempt，且执行窗口有重叠（capacity=2
        # 下公平调度并发执行，而非一串行等待另一个）。
        # 窗口重叠定义：a.started < b.ended 且 b.started < a.ended；若串行，
        # 后到者的 started 必然 >= 先到者的 ended。
        assert len(attempts_a) == 1 and len(attempts_b) == 1
        window_a = (attempts_a[0].started_at, attempts_a[0].ended_at)
        window_b = (attempts_b[0].started_at, attempts_b[0].ended_at)
        assert all(window_a) and all(window_b)
        assert window_a[0] < window_b[1] and window_b[0] < window_a[1], (
            f"执行窗口无重叠（串行执行）: {window_a} vs {window_b}"
        )
        # 各租户恰好 4 次 settled MCP 调用、钱包各结 40 分（互不侵占额度）
        calls_a = await _run_tool_calls(run_a_id)
        calls_b = await _run_tool_calls(run_b_id)
        if not (len(calls_a) == 4 and len(calls_b) == 4):
            # flake 现场留证：dump 两条 run 的调用/事件/attempt 供定性
            await _debug_dump_run(run_a_id, "fairness-A")
            await _debug_dump_run(run_b_id, "fairness-B")
        assert len(calls_a) == 4 and all(call.status == "settled" for call in calls_a)
        assert len(calls_b) == 4 and all(call.status == "settled" for call in calls_b)
        assert wallet_a is not None
        assert (wallet_a.balance, wallet_a.reserved) == (wallet_a_before.balance - 40, 0)
        assert wallet_b is not None
        assert (wallet_b.balance, wallet_b.reserved) == (wallet_b_before.balance - 40, 0)
        # 两个 fake 服务合计恰好 8 次真实外发（全部落在 insight-cube）
        assert len(topology.mcp_services[0].calls) == 8
        assert topology.mcp_services[1].calls == []


async def test_draining_gateway_stops_new_claims_but_finishes_active() -> None:
    """draining：在飞 Run 正常收口，新 queued Run 不被 claim；恢复 active 后可继续。"""
    topology = PiUatTopology(scripts={"slow": [step_hang(20), step_text("完成")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session1 = await topology.create_session(user)
        first = await topology.send_message(user, session1, "[scenario:slow]\n慢慢分析")
        assert first.status_code in (200, 201, 202), first.text
        run1_id = _message_run_id(first)
        # 等 run1 被 claim 进入 running，再置 draining（覆盖「在飞执行不收影响」）
        await _wait_run_status(run1_id, "running")
        await _set_gateway_mode(topology, "draining")
        # 确认 draining 已落库，再发第二条消息（新 session，避开会话互斥）
        from app.pi_gateway.models import PiGatewayInstance

        async with SessionFactory() as db:
            instance = await db.scalar(
                select(PiGatewayInstance).where(PiGatewayInstance.gateway_id == topology.gateway_id)
            )
        assert instance is not None and instance.mode == "draining"
        session2 = await topology.create_session(user)
        second = await topology.send_message(user, session2, "[scenario:slow]\n排队等待")
        assert second.status_code in (200, 201, 202), second.text
        run2_id = _message_run_id(second)
        assert run2_id != run1_id
        # 硬断言：draining 期间 run2 保持 queued 且 0 个 Attempt（claim 才会落
        # Attempt；scheduler.claim_next 在 mode != active 时直接返回空）。
        # 观测 4 秒，远超 gateway 的 100ms claim 轮询周期。
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            async with SessionFactory() as db:
                current = await db.get(AgentRun, run2_id)
                attempt_count = await db.scalar(
                    select(func.count())
                    .select_from(AgentRunAttempt)
                    .where(AgentRunAttempt.run_id == run2_id)
                )
            assert current is not None and current.status == "queued"
            assert attempt_count == 0
            await asyncio.sleep(0.3)
        # 在飞的 run1 不受 draining 影响，正常跑到 completed（心跳与终态不受 mode 限制）
        assert await topology.wait_run_terminal(run1_id, timeout=90) in (
            "completed",
            "completed_with_warnings",
        )
        # 恢复 active 后 run2 被 claim 并完成（证明 draining 可恢复、非永久拒派）
        await _set_gateway_mode(topology, "active")
        assert await topology.wait_run_terminal(run2_id, timeout=90) in (
            "completed",
            "completed_with_warnings",
        )
        async with SessionFactory() as db:
            attempts2 = list(
                (
                    await db.scalars(
                        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run2_id)
                    )
                ).all()
            )
        # run2 的首次 claim 必然发生在恢复 active 之后（draining 窗口内 0 Attempt）
        assert len(attempts2) == 1


async def test_current_to_pi_to_current_and_kill_switch_only_affects_new_runs() -> None:
    """灰度回切 + kill switch：只影响新 Run 的后端路由，旧 Run 快照逐字节不变。

    kill switch 的真实语义（读码确认，`tenancy/service.py` 的
    ``effective_runtime_backend`` + ``runtime_config/service.py`` 的
    ``snapshot_for_new_run``）：``PI_GATEWAY_KILL_SWITCH=true`` 时新 Run 的
    effective_backend 恒为 current——消息建单阶段就直接以 current 后端落库，
    而不是建成 pi Run 滞留队列（pi claim 只匹配 ``runtime_backend="pi"`` 的
    queued Run，kill switch 不在 claim 路径上）。因此 kill switch 是进程级
    启动配置，本测试用 ``restart_fastapi`` 在 pi Run 完成后才开启它。
    """
    topology = PiUatTopology(scripts={"refuse": [step_text("该问题超出营销分析范围。")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        user = tenant.users[0]
        # a) 租户从种子 pi 切回 current（admin PATCH 直切），发消息走 current
        # Runtime：fake-current-model 返回 complete 动作即时收口
        await _switch_runtime_backend(topology, tenant.tenant_id, "current")
        session_a = await topology.create_session(user)
        response = await topology.send_message(user, session_a, "帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run_a_id = _message_run_id(response)
        terminal_a = await topology.wait_run_terminal(run_a_id)
        if terminal_a not in ("completed", "completed_with_warnings"):
            await _debug_dump_run(run_a_id, "run_a")
        assert terminal_a in ("completed", "completed_with_warnings")
        async with SessionFactory() as db:
            row_a = await db.get(AgentRun, run_a_id)
        assert row_a is not None and row_a.runtime_backend == "current"
        snapshot_a = _canonical_snapshot(row_a)
        version_a = row_a.runtime_config_version_id
        # b) 切 pi 发拒答短脚本：经 gateway 真实 claim 执行
        config_id_b = await _enable_pi(topology, tenant.tenant_id)
        session_b = await topology.create_session(user)
        response = await topology.send_message(user, session_b, "[scenario:refuse]\n帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run_b_id = _message_run_id(response)
        terminal_b = await topology.wait_run_terminal(run_b_id)
        if terminal_b not in ("completed", "completed_with_warnings"):
            await _debug_dump_run(run_b_id, "run_b")
        assert terminal_b in ("completed", "completed_with_warnings")
        async with SessionFactory() as db:
            row_b = await db.get(AgentRun, run_b_id)
        assert row_b is not None and row_b.runtime_backend == "pi"
        assert row_b.runtime_config_version_id == config_id_b
        snapshot_b = _canonical_snapshot(row_b)
        # c) 开启 kill switch（进程级配置，重启 FastAPI 生效）后租户仍是 pi，
        # 但新消息建单即被改道 current：pi gateway 永远不会 claim 它。
        await topology.restart_fastapi(kill_switch=True)
        session_c = await topology.create_session(user)
        response = await topology.send_message(user, session_c, "[scenario:refuse]\n帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run_c_id = _message_run_id(response)
        terminal_c = await topology.wait_run_terminal(run_c_id)
        if terminal_c not in ("completed", "completed_with_warnings"):
            await _debug_dump_run(run_c_id, "run_c")
        assert terminal_c in ("completed", "completed_with_warnings")
        async with SessionFactory() as db:
            row_c = await db.get(AgentRun, run_c_id)
        # 硬断言：kill switch 下新 Run 以 current 建单并执行，全程未经 gateway
        assert row_c is not None and row_c.runtime_backend == "current"
        assert row_c.gateway_id is None
        assert row_c.gateway_lease_hash is None
        # d) 租户切回 current 再发一条：current 路径不受 kill switch 影响
        await _switch_runtime_backend(topology, tenant.tenant_id, "current")
        session_d = await topology.create_session(user)
        response = await topology.send_message(user, session_d, "帮我写周报")
        assert response.status_code in (200, 201, 202), response.text
        run_d_id = _message_run_id(response)
        terminal_d = await topology.wait_run_terminal(run_d_id)
        if terminal_d not in ("completed", "completed_with_warnings"):
            await _debug_dump_run(run_d_id, "run_d")
        assert terminal_d in ("completed", "completed_with_warnings")
        async with SessionFactory() as db:
            row_d = await db.get(AgentRun, run_d_id)
        assert row_d is not None and row_d.runtime_backend == "current"
        # e) 旧 Run 的 runtime_config_snapshot_json 在整个灰度回切 + kill switch
        # 过程后逐字节不变，版本指针不被改写
        async with SessionFactory() as db:
            final_a = await db.get(AgentRun, run_a_id)
            final_b = await db.get(AgentRun, run_b_id)
        assert final_a is not None and final_b is not None
        assert _canonical_snapshot(final_a) == snapshot_a
        assert final_a.runtime_config_version_id == version_a
        assert final_a.runtime_backend == "current"
        assert _canonical_snapshot(final_b) == snapshot_b
        assert final_b.runtime_config_version_id == config_id_b
        assert final_b.runtime_backend == "pi"


async def _read_sse_events(
    response: httpx.Response,
    *,
    want: int | None = None,
    until_terminal: bool = False,
) -> list[tuple[int, str, dict[str, Any]]]:
    """从 SSE 响应体解析事件帧（``id:`` = per-run sequence）；满足数量或终态即返回。"""
    terminal_types = {"run.completed", "run.completed_with_warnings", "run.failed", "run.cancelled"}
    events: list[tuple[int, str, dict[str, Any]]] = []
    frame: dict[str, str] = {}
    async for line in response.aiter_lines():
        line = line.rstrip("\r")
        if not line:
            if "id" in frame:
                events.append(
                    (int(frame["id"]), frame.get("event", ""), json.loads(frame.get("data", "{}")))
                )
                frame = {}
                if want is not None and len(events) >= want:
                    return events
                if until_terminal and events[-1][1] in terminal_types:
                    return events
            continue
        if line.startswith(":"):
            continue  # 心跳注释行
        key, _, value = line.partition(": ")
        frame[key] = value
    return events


async def test_sse_ordering_and_last_event_id_resume() -> None:
    """SSE：事件 id 单调递增、message.completed 先于终态；Last-Event-ID 续传无重复。"""
    # hang 6s 拉开事件时间分布：claim 时的早期事件与完成期事件之间有空窗，
    # 保证第一次连接在 Run 仍在 running 时断开，续传走真实「断线重连」路径。
    topology = PiUatTopology(scripts={"slow": [step_hang(6), step_text("慢速完成")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session_id = await topology.create_session(user)
        response = await topology.send_message(user, session_id, "[scenario:slow]\n慢慢分析")
        assert response.status_code in (200, 201, 202), response.text
        run_id = _message_run_id(response)
        async with topology.client_for(user) as client:
            # 第一次连接：收到首个事件即断开（Run 此时仍在 hang，远未终态）
            async with client.stream("GET", f"/api/v1/agent/runs/{run_id}/events") as stream1:
                assert stream1.status_code == 200, stream1
                assert stream1.headers["content-type"].startswith("text/event-stream")
                first_batch = await _read_sse_events(stream1, want=1)
            assert len(first_batch) == 1
            last_seen = first_batch[0][0]
            # 断开后 Run 继续执行到终态（续传前的等待不消费任何事件）
            assert await topology.wait_run_terminal(run_id, timeout=90) in (
                "completed",
                "completed_with_warnings",
            )
            # 带 Last-Event-ID 重连：从已确认序号之后续传直到终态事件
            async with client.stream(
                "GET",
                f"/api/v1/agent/runs/{run_id}/events",
                headers={"Last-Event-ID": str(last_seen)},
            ) as stream2:
                assert stream2.status_code == 200, stream2
                second_batch = await _read_sse_events(stream2, until_terminal=True)
        assert second_batch, "续传未收到任何后续事件"
        # 硬断言：续传不重发已确认事件（全部 id > last_seen），且自身单调递增
        second_ids = [event_id for event_id, _, _ in second_batch]
        assert all(event_id > last_seen for event_id in second_ids)
        assert second_ids == sorted(second_ids)
        # 硬断言：两批拼接恰好覆盖 1..N 连续无洞无重复（重放语义 =
        # 从 Last-Event-ID 之后续传，不是全量重放）
        combined = [first_batch[0][0], *second_ids]
        assert combined == list(range(1, combined[-1] + 1))
        # 终态事件在流末尾且 message.completed 先于它
        types = [event_type for _, event_type, _ in [*first_batch, *second_batch]]
        terminal_types = {
            "run.completed",
            "run.completed_with_warnings",
            "run.failed",
            "run.cancelled",
        }
        assert types[-1] in terminal_types
        assert "message.completed" in types
        assert types.index("message.completed") < len(types) - 1


async def test_run_snapshot_immutable_across_rollout() -> None:
    """pi Run 完成后灰度升级配置版本：旧 Run 快照逐字节不变、版本指针不改。"""
    topology = PiUatTopology(scripts={"refuse": [step_text("该问题超出营销分析范围。")]})
    async with topology:
        await _wait_gateway_registered(topology)
        tenant = topology.tenants["uat-tenant-a"]
        config_id_1 = await _enable_pi(topology, tenant.tenant_id)
        user = tenant.users[0]
        session1 = await topology.create_session(user)
        first = await topology.send_message(user, session1, "[scenario:refuse]\n帮我写周报")
        assert first.status_code in (200, 201, 202), first.text
        run1_id = _message_run_id(first)
        assert await topology.wait_run_terminal(run1_id) in (
            "completed",
            "completed_with_warnings",
        )
        async with SessionFactory() as db:
            row1 = await db.get(AgentRun, run1_id)
        assert row1 is not None and row1.runtime_backend == "pi"
        assert row1.runtime_config_version_id == config_id_1
        snapshot_1 = _canonical_snapshot(row1)
        # 灰度升级：同一租户再建并激活一个新版本配置（旧版本 retire）
        config_id_2 = await _create_activate_pi_config(topology, tenant.tenant_id)
        assert config_id_2 != config_id_1
        # 新 Run 绑定新版本（证明升级确实生效，对照组）
        session2 = await topology.create_session(user)
        second = await topology.send_message(user, session2, "[scenario:refuse]\n帮我写周报")
        assert second.status_code in (200, 201, 202), second.text
        run2_id = _message_run_id(second)
        assert await topology.wait_run_terminal(run2_id) in (
            "completed",
            "completed_with_warnings",
        )
        async with SessionFactory() as db:
            row2 = await db.get(AgentRun, run2_id)
            final1 = await db.get(AgentRun, run1_id)
        assert row2 is not None and row2.runtime_config_version_id == config_id_2
        # 硬断言：旧 Run 的 runtime_config_snapshot_json 逐字节不变、版本指针不改
        assert final1 is not None
        assert _canonical_snapshot(final1) == snapshot_1
        assert final1.runtime_config_version_id == config_id_1
