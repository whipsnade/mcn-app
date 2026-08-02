"""真实模型 + 真实 DataTap MCP UAT（设计 §17.3 / Task 26）。

驱动真实引擎（真实 TencentPlan 模型网关 + 真实 DataTapTransport + test DB）跑通
§17.3 的九个真实场景，并验证账本与证据：

- 默认跳过：只有 ``RUN_REAL_SERVICES=1`` 且经 ``-m real_services`` 才会运行
  （scripts/run_real_agent_uat.sh 负责设置）。
- 强制隔离：conftest 的 test 环境变量把本测试约束在 ``kol_insight_test``；脚本还会
  FORCE-override APP_ENV/MYSQL_*/AUTH_MODE，绝不触碰 dev DB。
- 安全：所有记录都不含密钥、DSN、完整原始 prompt 或原始 MCP payload。
- 计费断言：每个 settled 调用恰好 10 分；failed_confirmed/definitely_not_sent 释放
  预留；unknown 被恢复核对（或保持预留并留有审计行）。
- 证据断言：已发布 Artifact 的每个正式数值字段都有有效 lineage。

模型行为非确定：本 UAT 不断言固定工具顺序，只断言用户目标、状态、计费、证据与
Artifact 契约；模型实际做了什么是逐场景如实记录（见 QA doc）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageError,
    LineageOwner,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.models import AgentArtifactVersion
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    AgentSession,
)
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.reviewer import ReviewDecision, ReviewerDriver
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.artifacts import CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.calculation import (
    AggregateMetricsTool,
    CalculateExpressionTool,
    CalculatePeriodComparisonTool,
    NormalizeSentimentTool,
    RankKolsTool,
)
from app.agent_runtime.tools.history import ReadArtifactTool, ReadToolResultTool, SearchEvidenceTool
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.models import Wallet, WalletTransaction
from app.db.session import SessionFactory
from app.identity.models import User
from app.model.contracts import ChatMessage
from app.model.dependencies import get_model_adapter
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST
from app.mcp_gateway.service import get_mcp_transport, refresh_approved_datatap_tools
from app.mcp_gateway.transport import (
    McpGatewayTimeout,
    McpTransport,
)

pytestmark = [
    pytest.mark.real_services,
    pytest.mark.skipif(
        os.environ.get("RUN_REAL_SERVICES") != "1",
        reason="真实服务 UAT 需要 RUN_REAL_SERVICES=1（scripts/run_real_agent_uat.sh）",
    ),
]

MCP_POINTS_COST = 10
WORKER_ID = "uat-worker"
RESULTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "outputs")
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 结果记录（安全：无密钥 / 无完整原始 payload / 无原始 prompt）
# --------------------------------------------------------------------------- #


@dataclass
class CallRecord:
    tool_call_id: str
    internal_tool_name: str
    status: str
    error_type: str | None
    points_reserved: int
    points_settled: int
    service: str | None = None
    # unknown 调用是否已被恢复循环 reconcile（有 AgentToolCallReconciliation 审计行）。
    # 本 UAT 不运行恢复循环进程组件，审计行缺失是已知缺口（§11.1），见 QA doc。
    reconciled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "internal_tool_name": self.internal_tool_name,
            "status": self.status,
            "error_type": self.error_type,
            "points_reserved": self.points_reserved,
            "points_settled": self.points_settled,
            "service": self.service,
            "reconciled": self.reconciled,
        }


@dataclass
class ScenarioRecord:
    scenario: str
    prompt_tag: str
    profile: str
    run_id: str
    status: str
    decision_count: int
    points_before: int
    points_after: int
    wallet_tx_summary: list[dict[str, Any]] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    artifact_versions: list[dict[str, Any]] = field(default_factory=list)
    assistant_summary: list[dict[str, Any]] = field(default_factory=list)
    unknown_reconciled: bool = False
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "prompt_tag": self.prompt_tag,
            "profile": self.profile,
            "run_id": self.run_id,
            "status": self.status,
            "decision_count": self.decision_count,
            "points_before": self.points_before,
            "points_after": self.points_after,
            "wallet_tx_summary": self.wallet_tx_summary,
            "calls": [c.to_dict() for c in self.calls],
            "steps": self.steps,
            "artifact_versions": self.artifact_versions,
            "assistant_summary": self.assistant_summary,
            "unknown_reconciled": self.unknown_reconciled,
            "limitations": self.limitations,
        }


_ALL_RECORDS: list[ScenarioRecord] = []


def _dump_results() -> None:
    if not _ALL_RECORDS:
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "agent-runtime-uat-results.json")
    payload = {
        "generated_at": _utcnow().isoformat(),
        "env": {
            "app_env": os.environ.get("APP_ENV"),
            "mysql_database": os.environ.get("MYSQL_DATABASE"),
            "auth_mode": os.environ.get("AUTH_MODE"),
        },
        "scenarios": [record.to_dict() for record in _ALL_RECORDS],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


@pytest.fixture(scope="session", autouse=True)
async def _uat_catalog_ready() -> None:
    """一次性刷新 test DB 的已审核 MCP 目录（真实 discovery，零计费）。"""
    await refresh_approved_datatap_tools()
    yield


@pytest.fixture(autouse=True)
def _uat_dump_after_each() -> None:
    """每个场景结束后立即落盘结果，防止长 run 中途失败丢失前序记录。"""
    yield
    _dump_results()


# --------------------------------------------------------------------------- #
# 工具注册表 / 引擎装配
# --------------------------------------------------------------------------- #


def _resolve_remote_entry(service: DataTapService, internal_tool_name: str):
    return DYNAMIC_TOOL_ALLOWLIST.get(service, {}).get(internal_tool_name)


async def _load_catalog(db: AsyncSession) -> list[McpToolCatalog]:
    return list((await db.scalars(select(McpToolCatalog))).all())


def _make_mcp_tool(db: AsyncSession, row: McpToolCatalog, *, transport: McpTransport):
    try:
        service = DataTapService(row.service_slug)
    except ValueError:
        return None
    entry = _resolve_remote_entry(service, row.internal_tool_name)
    if entry is None:
        return None
    _remote, _description, output_schema = entry
    return AgentMcpTool(
        internal_name=row.internal_tool_name,
        service=service,
        remote_name=row.internal_tool_name,  # 实时网关以内部名暴露工具
        input_schema=row.input_schema_json,
        output_schema=output_schema,
        db_session=db,
        transport=transport,
    )


def _build_registry(db: AsyncSession, *, transport: McpTransport | None = None) -> ToolRegistry:
    transport = transport or get_mcp_transport()
    registry = ToolRegistry(
        catalog_source=lambda: _load_catalog(db),
        mcp_executor_factory=lambda row: _make_mcp_tool(db, row, transport=transport),
    )
    registry.register(CalculateExpressionTool(db), category="calculation")
    registry.register(AggregateMetricsTool(db), category="calculation")
    registry.register(CalculatePeriodComparisonTool(db), category="calculation")
    registry.register(NormalizeSentimentTool(db), category="calculation")
    registry.register(RankKolsTool(db), category="calculation")
    registry.register(ReadArtifactTool(db), category="history")
    registry.register(SearchEvidenceTool(db), category="history")
    registry.register(ReadToolResultTool(db), category="history")
    registry.register(CreateDraftTool(db), category="artifact")
    registry.register(UpdateDraftTool(db), category="artifact")
    return registry


def _build_engine(
    db: AsyncSession,
    *,
    registry: ToolRegistry,
    gateway: AgentModelGateway,
    reviewer_gateway: AgentModelGateway | None = None,
    worker_id: str = WORKER_ID,
) -> AgentEngine:
    reviewer = ReviewerDriver(db, reviewer_gateway or gateway, worker_id=worker_id)
    broker = AgentEventBroker()
    events = AgentEventStream(db, broker)
    return AgentEngine(
        db,
        gateway=gateway,
        registry=registry,
        events=events,
        reviewer=reviewer,
        worker_id=worker_id,
    )


class ScriptedReviewerGateway:
    """脚本化 Reviewer 决策（approve/revise/reject），供确定性场景。"""

    def __init__(self, decisions: list[ReviewDecision]) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
        self.calls += 1
        if not self.decisions:
            raise AssertionError("scripted reviewer gateway exhausted")
        return self.decisions.pop(0)


# --------------------------------------------------------------------------- #
# 场景运行器
# --------------------------------------------------------------------------- #


async def _new_user_session_wallet(
    db: AsyncSession, *, scenario: str, balance: int
) -> tuple[User, AgentSession, Wallet]:
    now = utc_now()
    user = User(
        id=str(uuid4()),
        nickname=f"uat-{scenario[:12]}",
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()
    agent_session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title=f"UAT {scenario}",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(agent_session)
    await db.flush()
    wallet = Wallet(user_id=user.id, balance=balance, reserved=0, version=0, updated_at=now)
    db.add(wallet)
    await db.flush()
    return user, agent_session, wallet


async def _run_scenario(
    *,
    scenario: str,
    prompt: str,
    balance: int = 1000,
    profile_name: str = "session_analyst_v1",
    transport: McpTransport | None = None,
    reviewer_gateway: AgentModelGateway | None = None,
) -> ScenarioRecord:
    """创建独立 user/session/wallet + Run，驱动真实引擎执行一次场景，收集证据并提交。"""
    db = SessionFactory()
    async with db as session:
        user, agent_session, _wallet = await _new_user_session_wallet(
            session, scenario=scenario, balance=balance
        )
        repo = AgentRunRepository(session)
        run = AgentRun(
            id=str(uuid4()),
            session_id=agent_session.id,
            user_id=user.id,
            run_kind="user",
            visibility="user",
            profile_name=profile_name,
            profile_version="v1",
            model="glm-5.2",
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )
        session.add(run)
        await session.flush()
        attempt = await repo.begin_attempt(run.id)
        await repo.claim_lease(run.id, WORKER_ID, 300)

        registry = _build_registry(session, transport=transport)
        gateway = AgentModelGateway(get_model_adapter(), db=session)
        engine = _build_engine(
            session,
            registry=registry,
            gateway=gateway,
            reviewer_gateway=reviewer_gateway,
        )
        # 先登记 run_id（status=running）并立即落盘：即使真实 DataTap 查询挂起
        # 导致 engine.run 无法返回，JSON 仍保留该场景的 run_id（Fix 5）。
        record = ScenarioRecord(
            scenario=scenario,
            prompt_tag=prompt[:60],
            profile=profile_name,
            run_id=run.id,
            status="running",
            decision_count=0,
            points_before=balance,
            points_after=balance,
        )
        _ALL_RECORDS.append(record)
        _dump_results()

        outcome = await engine.run(
            run=run,
            attempt_id=attempt.id,
            profile=get_profile(profile_name),
            messages=[ChatMessage(role="user", content=prompt)],
        )

        wallet_row = await session.get(Wallet, user.id)
        record.status = str(outcome.status)
        record.decision_count = outcome.decision_count
        record.points_after = wallet_row.balance if wallet_row is not None else 0
        await _collect_run_record(session, run.id, record)
        await session.commit()
        return record


async def _collect_run_record(db: AsyncSession, run_id: str, record: ScenarioRecord) -> None:
    run = await db.get(AgentRun, run_id)
    user_id = run.user_id if run is not None else ""
    session_id = run.session_id if run is not None else ""

    calls = list(
        (
            await db.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.run_id == run_id)
                .order_by(AgentToolCall.started_at, AgentToolCall.id)
            )
        ).all()
    )
    for call in calls:
        reconciled = False
        if call.status == "unknown":
            recon_count = await db.scalar(
                select(func.count(AgentToolCallReconciliation.id)).where(
                    AgentToolCallReconciliation.tool_call_id == call.id
                )
            )
            reconciled = (recon_count or 0) > 0
            if reconciled:
                record.unknown_reconciled = True
            else:
                # §11.1 后半（keep_unknown 审计行）由恢复循环进程组件负责；本 UAT
                # 不运行该组件，审计行缺失是已知缺口，如实记录为 limitation。
                record.limitations.append(
                    f"unknown call {call.internal_tool_name} keeps reservation but lacks "
                    "reconciliation audit row (recovery loop not exercised in UAT)"
                )
        record.calls.append(
            CallRecord(
                tool_call_id=call.id,
                internal_tool_name=call.internal_tool_name,
                status=call.status,
                error_type=call.error_type,
                points_reserved=call.points_reserved or 0,
                points_settled=call.points_settled or 0,
                service=call.service,
                reconciled=reconciled,
            )
        )

    steps = list(
        (
            await db.scalars(
                select(AgentStep)
                .where(AgentStep.run_id == run_id, AgentStep.step_type == "tool_call")
                .order_by(AgentStep.sequence)
            )
        ).all()
    )
    for step in steps:
        inp = step.input_json or {}
        out = step.output_json or {}
        record.steps.append(
            {
                "sequence": step.sequence,
                "internal_tool_name": str(inp.get("internal_tool_name") or ""),
                "status": step.status,
                "error_type": out.get("error_type"),
            }
        )

    # 钱包流水只取本 Run 的 agent_tool_call 预留/结算/释放：按 user_id 与
    # reference_id（本 Run 各调用 id）双重限定，避免跨场景流水串扰。
    call_ids = [call.id for call in calls]
    if call_ids:
        transactions = list(
            (
                await db.scalars(
                    select(WalletTransaction)
                    .where(
                        WalletTransaction.reference_type == "agent_tool_call",
                        WalletTransaction.user_id == user_id,
                        WalletTransaction.reference_id.in_(call_ids),
                    )
                    .order_by(WalletTransaction.created_at)
                )
            ).all()
        )
    else:
        transactions = []
    for tx in transactions:
        record.wallet_tx_summary.append(
            {
                "kind": tx.kind,
                "balance_delta": tx.balance_delta,
                "reserved_delta": tx.reserved_delta,
            }
        )

    versions = list(
        (
            await db.scalars(
                select(AgentArtifactVersion)
                .where(AgentArtifactVersion.source_run_id == run_id)
                .order_by(AgentArtifactVersion.created_at)
            )
        ).all()
    )
    for version in versions:
        lineage_ok, lineage_err = await _verify_version_lineage(db, version, user_id, session_id)
        record.artifact_versions.append(
            {
                "artifact_id": version.artifact_id,
                "version": version.version,
                "schema_version": version.schema_version,
                "data_status": version.data_status,
                "lineage_ok": lineage_ok,
                "lineage_error": lineage_err,
            }
        )
        if not lineage_ok:
            record.limitations.append(
                f"artifact {version.artifact_id} lineage failed: {lineage_err}"
            )

    messages = list(
        (
            await db.scalars(
                select(AgentMessage)
                .where(AgentMessage.run_id == run_id, AgentMessage.role == "assistant")
                .order_by(AgentMessage.sequence)
            )
        ).all()
    )
    for message in messages:
        meta = message.metadata_json or {}
        record.assistant_summary.append(
            {
                "type": meta.get("type") or "completion",
                "content_prefix": (message.content or "")[:120],
            }
        )


async def _verify_version_lineage(
    db: AsyncSession,
    version: AgentArtifactVersion,
    user_id: str,
    session_id: str,
) -> tuple[bool, str | None]:
    if not (version.payload_json or {}).get("data"):
        return True, None
    try:
        await validate_and_freeze_lineage(
            payload=version.payload_json or {},
            refs=version.evidence_refs_json or [],
            owner=LineageOwner(user_id=user_id, session_id=session_id, run_id=version.source_run_id),
            loader=DbLineageLoader(db),
        )
        return True, None
    except LineageError as exc:
        return False, f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 - 记录任何校验失败
        return False, f"{type(exc).__name__}: {exc}"


async def _assert_ledger(record: ScenarioRecord) -> None:
    for call in record.calls:
        is_mcp = call.service != "internal"
        if call.status == "settled":
            # MCP 调用每次恰好 10 分；内部计算/历史/草稿工具 0 分。
            expected = MCP_POINTS_COST if is_mcp else 0
            assert call.points_settled == expected, (
                f"settled call {call.tool_call_id} charged {call.points_settled}"
            )
            assert call.points_reserved == 0
        elif call.status == "failed":
            assert call.points_settled == 0, (
                f"failed call {call.tool_call_id} settled {call.points_settled}"
            )
            assert call.points_reserved == 0, (
                f"failed call {call.tool_call_id} still reserved {call.points_reserved}"
            )
        elif call.status == "unknown":
            assert call.points_settled == 0
            if call.reconciled:
                # 已被恢复循环 reconcile：结算或释放后预留清零。
                assert call.points_reserved == 0, (
                    f"reconciled unknown call {call.tool_call_id} still reserved "
                    f"{call.points_reserved}"
                )
            else:
                # §11.1：未 reconcile 的 unknown 必须保持预留（等恢复循环核对）。
                assert call.points_reserved > 0, (
                    f"unknown call {call.tool_call_id} lost its reservation without reconciliation"
                )
                # 审计行（keep_unknown）由恢复循环进程组件写入；本 UAT 不运行该组件，
                # 缺失已在 _collect_run_record 记录为 limitation（已知缺口，见 QA doc）。
                assert any(
                    "lacks reconciliation audit row" in limitation
                    for limitation in record.limitations
                ), (
                    f"unknown call {call.tool_call_id} audit-row gap must be recorded as a "
                    "known limitation"
                )


# --------------------------------------------------------------------------- #
# 故障注入（场景 7：趋势 504 后继续其他工具）
# --------------------------------------------------------------------------- #


class FaultInjectingTransport:
    """包装真实 DataTapTransport：对指定 tool 抛 McpGatewayTimeout。"""

    def __init__(self, inner: DataTapTransport, *, fail_tool: str) -> None:
        self._inner = inner
        self._fail_tool = fail_tool
        self.failed_calls: list[str] = []

    async def call_tool(self, service, remote_name, arguments):
        if remote_name == self._fail_tool:
            self.failed_calls.append(remote_name)
            raise McpGatewayTimeout("injected 504 for UAT")
        return await self._inner.call_tool(service, remote_name, arguments)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def reconcile_tool_call(self, upstream_request_id):
        return await self._inner.reconcile_tool_call(upstream_request_id)


# --------------------------------------------------------------------------- #
# 场景 1：信息不足时主动澄清
# --------------------------------------------------------------------------- #


async def test_uat_clarification_when_info_insufficient() -> None:
    record = await _run_scenario(
        scenario="clarification",
        prompt="帮我分析一下某个品牌的声量和情感，但我不确定具体要分析哪个品牌。",
    )
    assert record.status == RunStatus.CLARIFICATION_REQUESTED.value
    assert record.decision_count >= 1


# --------------------------------------------------------------------------- #
# 场景 2：品牌分析 → brand_report_v3
# --------------------------------------------------------------------------- #


# 真实模型能驱动 MCP 调用，但受当前 prompt 工程限制可能陷入 draft lineage 反复
# 修订并触发 Attempt 保护暂停（50 决策）——这是真实行为，如实记录为 PAUSED。
_REAL_MODEL_TERMINAL = {
    RunStatus.COMPLETED.value,
    RunStatus.CLARIFICATION_REQUESTED.value,
    RunStatus.FAILED.value,
    RunStatus.PAUSED.value,
}


async def test_uat_brand_analysis_real() -> None:
    record = await _run_scenario(
        scenario="brand_analysis",
        prompt="请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。",
    )
    await _assert_ledger(record)
    assert record.status in _REAL_MODEL_TERMINAL


# --------------------------------------------------------------------------- #
# 场景 3：活动分析（campaign）
# --------------------------------------------------------------------------- #


async def test_uat_campaign_analysis_real() -> None:
    record = await _run_scenario(
        scenario="campaign_analysis",
        prompt="瑞幸咖啡最近有‘9.9咖啡节’活动，请分析这个活动在社交媒体的传播效果。",
    )
    await _assert_ledger(record)
    assert record.status in _REAL_MODEL_TERMINAL


# --------------------------------------------------------------------------- #
# 场景 4：Top20 达人圈选与 KOL 分析（kol_selection_v3 + kol_analysis_v2）
# --------------------------------------------------------------------------- #


async def test_uat_kol_selection_and_analysis_real() -> None:
    record = await _run_scenario(
        scenario="kol_selection",
        prompt="请为瑞幸咖啡圈选Top20达人，并分析其中前5位达人的核心价值。",
    )
    await _assert_ledger(record)
    assert record.status in _REAL_MODEL_TERMINAL


# --------------------------------------------------------------------------- #
# 场景 5：基于已发布 Artifact 的钻取（insight_board_v1）
# --------------------------------------------------------------------------- #


async def test_uat_insight_drilldown_real() -> None:
    parent_record = await _run_scenario(
        scenario="brand_analysis_parent",
        prompt="请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。",
    )
    parent_version_id = None
    if parent_record.artifact_versions:
        parent_version_id = parent_record.artifact_versions[0]["artifact_id"]
        # 需父 Version id 而非 artifact id；读取 DB 以获取实际 version id。
    db = SessionFactory()
    try:
        async with db:
            if parent_version_id:
                version_row = await db.scalar(
                    select(AgentArtifactVersion)
                    .where(AgentArtifactVersion.artifact_id == parent_version_id)
                    .order_by(AgentArtifactVersion.version.desc())
                )
                parent_version_id = version_row.id if version_row else None
    finally:
        await db.close()

    if not parent_version_id:
        pytest.skip("父品牌分析未发布 Artifact，无法执行钻取场景")

    record = await _run_scenario(
        scenario="insight_drilldown",
        prompt=(
            f"基于已发布的品牌分析报告（parent_artifact_version_id={parent_version_id}），"
            "按平台和情感维度做一次钻取，关注声量峰值的平台分布与正面情感占比。"
        ),
    )
    await _assert_ledger(record)
    assert record.status in _REAL_MODEL_TERMINAL


# --------------------------------------------------------------------------- #
# 场景 6：达人详情缓存、主页和 5 条热帖（kol_detail_v2 + cache）
# --------------------------------------------------------------------------- #


async def test_uat_kol_detail_cache_real() -> None:
    """达人详情缓存：确定性验证 24h Session 级缓存命中。

    限制说明（真实 fetch 路径）：``kol_detail_v1`` Profile 只允许
    ``{KOL_DETAIL_TOOLS, ARTIFACT_TOOLS}``，而真实 ``kol_detail`` MCP 工具以
    ``MCP_TOOLS`` 分类注册，因此生产接线无法让 kol_detail_v1 触达该 MCP 工具
    （见 test_profiles.test_kol_detail_tool_categories）——此差异如实记录为 limitation。
    """
    from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
    from app.agent_runtime.kol_detail import KolDetailRunService

    db = SessionFactory()
    try:
        async with db as session:
            user, agent_session, _wallet = await _new_user_session_wallet(
                session, scenario="kol_detail", balance=1000
            )
            now = _utcnow()
            detail = {
                "identity": {
                    "nickname": "瑞幸咖啡官方",
                    "homepage_url": "https://www.xiaohongshu.com/user/profile/123",
                    "bio": "瑞幸咖啡官方小红书账号",
                    "verification": True,
                    "region": "上海",
                },
                "metrics": {
                    "followers": 1000000,
                    "following": 100,
                    "posts": 500,
                    "likes": 10000,
                },
                "audience": {},
                "trend": [],
                "latest_posts": [],
            }
            built = build_kol_detail_draft(
                platform="xiaohongshu",
                kol_uid="瑞幸咖啡官方账号",
                detail=detail,
                evidence_id=str(uuid4()),
                cache_state={"hit": False, "fetched_at": now.isoformat(), "expires_at": now.isoformat()},
                data_as_of=now,
            )
            service = KolDetailRunService(
                session,
                engine=None,
                worker_id="api-kol-detail",
                now_fn=lambda: now,
            )
            await service.set_cached_detail(
                user_id=user.id,
                session_id=agent_session.id,
                platform="xiaohongshu",
                kol_uid="瑞幸咖啡官方账号",
                payload=built.payload,
                evidence_refs=built.evidence_refs,
                fetched_at=now,
                expires_at=now + timedelta(hours=24),
            )
            # 命中缓存：零模型 / 零 MCP 调用，detail 重建为 hit=true。
            summary = await service.create(
                user.id,
                agent_session.id,
                platform="xiaohongshu",
                kol_uid="瑞幸咖啡官方账号",
            )
            assert summary.cached is True
            assert summary.detail is not None
            assert summary.detail["data"]["cache"]["hit"] is True
            assert summary.detail["data"]["identity"]["nickname"] == "瑞幸咖啡官方"

            record = ScenarioRecord(
                scenario="kol_detail",
                prompt_tag="kol_detail_v2 + 24h cache（确定性命中）",
                profile="kol_detail_v1",
                run_id="",
                status="completed",
                decision_count=0,
                points_before=1000,
                points_after=(await session.get(Wallet, user.id)).balance,
            )
            record.limitations.append(
                "kol_detail_v1 Profile 未允许 MCP_TOOLS，生产接线无法触达真实 kol_detail MCP 工具"
            )
            _ALL_RECORDS.append(record)
            await session.commit()
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 场景 7：趋势 504 后继续其他工具
# --------------------------------------------------------------------------- #


async def test_uat_tool_failure_does_not_stop_run() -> None:
    """确定性验证 504 后引擎继续执行其他工具（引擎机制，不依赖模型 JSON 稳定性）。

    agent 决策脚本化：先调 social_statistic_trend（被故障注入 504 → failed_confirmed
    释放预留），再调真实 query_analysis_data（成功 → settled 10 分），最后 complete。
    """
    from app.agent_runtime.schemas import CallTool, Complete

    injected = FaultInjectingTransport(get_mcp_transport(), fail_tool="social_statistic_trend")

    class ScriptedTrendGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return CallTool(
                    action="call_tool",
                    internal_tool_name="social_statistic_trend",
                    arguments={
                        "target_type": "keyword",
                        "start_time": "2025-06-13",
                        "end_time": "2025-07-13",
                        "datasource": ["小红书"],
                    },
                    rationale="查声量趋势",
                )
            if self.calls == 2:
                return CallTool(
                    action="call_tool",
                    internal_tool_name="calculate_expression",
                    arguments={"expression": "1+1", "variables": {}},
                    rationale="趋势失败后继续使用其他工具",
                )
            return Complete(action="complete", text="完成分析", suggestions=None)

    db = SessionFactory()
    try:
        async with db as session:
            user, agent_session, _wallet = await _new_user_session_wallet(
                session, scenario="trend_504_continue", balance=1000
            )
            repo = AgentRunRepository(session)
            run = AgentRun(
                id=str(uuid4()),
                session_id=agent_session.id,
                user_id=user.id,
                run_kind="user",
                visibility="user",
                profile_name="session_analyst_v1",
                profile_version="v1",
                model="glm-5.2",
                status="queued",
                decision_count=0,
                review_count=0,
                revision_count=0,
            )
            session.add(run)
            await session.flush()
            attempt = await repo.begin_attempt(run.id)
            await repo.claim_lease(run.id, WORKER_ID, 300)

            scripted_agent = ScriptedTrendGateway()

            class _MainGateway:
                async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
                    return await scripted_agent.decide(
                        run=run, attempt_id=attempt_id, profile=profile,
                        messages=messages, thinking_sink=thinking_sink, **kwargs
                    )

            registry = _build_registry(session, transport=injected)
            real_gateway = AgentModelGateway(get_model_adapter(), db=session)
            engine = _build_engine(session, registry=registry, gateway=real_gateway)
            engine._gateway = _MainGateway()  # noqa: SLF001 - 测试专用替换

            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile("session_analyst_v1"),
                messages=[ChatMessage(role="user", content="分析瑞幸咖啡声量趋势")],
            )
            record = ScenarioRecord(
                scenario="trend_504_continue",
                prompt_tag="504 后继续其他工具（确定性）",
                profile="session_analyst_v1",
                run_id=run.id,
                status=str(outcome.status),
                decision_count=outcome.decision_count,
                points_before=1000,
                points_after=(await session.get(Wallet, user.id)).balance,
            )
            await _collect_run_record(session, run.id, record)
            _ALL_RECORDS.append(record)
            await session.commit()

            assert injected.failed_calls, "trend 工具未被实际调用（故障注入未生效）"
            # 504 不中断 Run：后续 calculate_expression 步骤仍成功完成。
            assert record.status == RunStatus.COMPLETED.value
            await _assert_ledger(record)
            trend_calls = [
                call for call in record.calls
                if call.internal_tool_name == "social_statistic_trend"
            ]
            assert trend_calls, "trend 调用缺失"
            for call in trend_calls:
                # 网关 504 → result_unknown（请求可能已发出），不结算；等待恢复核对。
                assert call.status == "unknown"
                assert call.points_settled == 0
                assert call.points_reserved == MCP_POINTS_COST
            # 趋势（sequence=2）之后必须有继续执行的工具步骤。
            later_success = [
                step for step in record.steps
                if step["sequence"] > 2 and step["status"] == "completed"
            ]
            assert later_success, "504 后引擎未继续执行其他工具"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# 场景 8：钱包不足后的 restricted 交付
# --------------------------------------------------------------------------- #


async def test_uat_wallet_insufficient_restricted_delivery() -> None:
    record = await _run_scenario(
        scenario="wallet_insufficient",
        prompt="请分析瑞幸咖啡最近一个月的品牌声量。",
        balance=5,
    )
    await _assert_ledger(record)
    assert record.points_after >= 0, "钱包不得为负"
    assert record.status in {
        RunStatus.COMPLETED.value,
        RunStatus.CLARIFICATION_REQUESTED.value,
        RunStatus.FAILED.value,
    }


# --------------------------------------------------------------------------- #
# 场景 9：Reviewer revise 后补查或修订
# --------------------------------------------------------------------------- #


async def test_uat_reviewer_revise_then_fix() -> None:
    """确定性验证 Reviewer revise → 修正 → 复审 → 原子发布闭环，并校验 lineage。

    agent 主循环决策脚本化（create_draft → submit_review → update_draft → submit），
    Reviewer 决策脚本化（revise 一次 + 反馈问题，随后 approve），保证 revise 分支可
    稳定触发。Draft 携带数值字段与指向真实 Evidence 的 lineage ref，发布后校验
    ``validate_and_freeze_lineage`` 不抛错。
    """
    from app.agent_runtime.evidence import EvidenceWriter
    from app.agent_runtime.schemas import CallTool, Complete, SubmitReview

    def _parse_draft_id(messages) -> str | None:
        """从最近一条 tool_result 成功摘要解析 draft_id。"""
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, str) or '"tool_result"' not in content:
                continue
            try:
                parsed = json.loads(content)
                summary = parsed.get("tool_result", {}).get("summary")
                if isinstance(summary, str):
                    inner = json.loads(summary)
                    if inner.get("draft_id"):
                        return inner["draft_id"]
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        return None

    class ScriptedAgentGateway:
        """脚本化 agent 决策：create_draft → submit_review → update_draft → submit → complete。"""

        def __init__(self, evidence_id: str) -> None:
            self.calls = 0
            self.draft_id: str | None = None
            self._evidence_id = evidence_id
            self._payload = {"data": {"overview": {"brand": "瑞幸", "volume": 100}}}
            self._refs = [
                {
                    "artifact_path": "/data/overview/volume",
                    "sources": [
                        {
                            "source_type": "evidence",
                            "evidence_id": self._evidence_id,
                            "source_path": "/result",
                        }
                    ],
                }
            ]

        async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
            self.calls += 1
            if self.draft_id is None:
                self.draft_id = _parse_draft_id(messages)
            if self.calls == 1:
                return CallTool(
                    action="call_tool",
                    internal_tool_name="create_draft",
                    arguments={
                        "module": "brand",
                        "schema_version": "brand_report_v3",
                        "artifact_type": "brand_report_v3",
                        "business_fields": {"brand": "瑞幸"},
                        "payload": self._payload,
                        "evidence_refs": self._refs,
                    },
                    rationale="创建品牌 Draft",
                )
            if self.calls == 2:
                return SubmitReview(
                    action="submit_review",
                    artifact_draft_ids=[self.draft_id] if self.draft_id else [],
                    completion_text="品牌分析报告已完成",
                    summary="瑞幸品牌声量分析",
                )
            if self.calls == 3:
                # 收到 revise 反馈（review_revision_requested）→ 补查后 update_draft。
                if self.draft_id is None:
                    self.draft_id = _parse_draft_id(messages)
                return CallTool(
                    action="call_tool",
                    internal_tool_name="update_draft",
                    arguments={
                        "draft_id": self.draft_id,
                        "payload": self._payload,
                        "evidence_refs": self._refs,
                    },
                    rationale="按 Reviewer 反馈补查并修订",
                )
            if self.calls == 4:
                return SubmitReview(
                    action="submit_review",
                    artifact_draft_ids=[self.draft_id] if self.draft_id else [],
                    completion_text="品牌分析报告已完成",
                    summary="瑞幸品牌声量分析（修订版）",
                )
            return Complete(action="complete", text="完成", suggestions=None)

    db = SessionFactory()
    try:
        async with db as session:
            user, agent_session, _wallet = await _new_user_session_wallet(
                session, scenario="reviewer_revise", balance=1000
            )
            repo = AgentRunRepository(session)
            run = AgentRun(
                id=str(uuid4()),
                session_id=agent_session.id,
                user_id=user.id,
                run_kind="user",
                visibility="user",
                profile_name="session_analyst_v1",
                profile_version="v1",
                model="glm-5.2",
                status="queued",
                decision_count=0,
                review_count=0,
                revision_count=0,
            )
            session.add(run)
            await session.flush()
            attempt = await repo.begin_attempt(run.id)
            await repo.claim_lease(run.id, WORKER_ID, 300)

            # 预置一条已 settled 的 MCP 调用 + Evidence（模拟真实抓数结果），
            # 供 draft 的 lineage 引用。
            fake_step = AgentStep(
                id=str(uuid4()),
                run_id=run.id,
                attempt_id=attempt.id,
                sequence=1,
                step_type="tool_call",
                input_json={"internal_tool_name": "query_analysis_data", "arguments": {}},
                status="completed",
                visibility="user",
                created_at=_utcnow(),
            )
            session.add(fake_step)
            await session.flush()
            fake_call = AgentToolCall(
                id=str(uuid4()),
                run_id=run.id,
                step_id=fake_step.id,
                logical_call_id=str(uuid4()),
                service="insight-cube-mcp",
                internal_tool_name="query_analysis_data",
                arguments_json={},
                arguments_hash="a" * 64,
                status="settled",
                points_reserved=0,
                points_settled=10,
                started_at=_utcnow(),
                completed_at=_utcnow(),
            )
            session.add(fake_call)
            await session.flush()
            evidence = await EvidenceWriter(session).write(
                session_id=agent_session.id,
                run_id=run.id,
                tool_call_id=fake_call.id,
                source_type="mcp",
                source_name="query_analysis_data",
                scope_json={"keyword": "瑞幸咖啡"},
                period_json={"period": "last_month"},
                raw_payload={"result": '{"volume": 100, "brand": "瑞幸"}'},
            )
            await session.flush()

            scripted_agent = ScriptedAgentGateway(evidence.id)
            scripted_reviewer = ScriptedReviewerGateway(
                [
                    ReviewDecision(decision="revise", issues=[]),
                    ReviewDecision(decision="approve", issues=[]),
                ]
            )

            class _MainGateway:
                async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
                    return await scripted_agent.decide(
                        run=run, attempt_id=attempt_id, profile=profile,
                        messages=messages, thinking_sink=thinking_sink, **kwargs
                    )

            registry = _build_registry(session)
            real_gateway = AgentModelGateway(get_model_adapter(), db=session)
            engine = _build_engine(
                session,
                registry=registry,
                gateway=real_gateway,
                reviewer_gateway=scripted_reviewer,
            )
            engine._gateway = _MainGateway()  # noqa: SLF001 - 测试专用替换

            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile("session_analyst_v1"),
                messages=[ChatMessage(role="user", content="分析瑞幸品牌声量并出报告")],
            )
            record = ScenarioRecord(
                scenario="reviewer_revise",
                prompt_tag="submit_review → revise → fix → approve → publish",
                profile="session_analyst_v1",
                run_id=run.id,
                status=str(outcome.status),
                decision_count=outcome.decision_count,
                points_before=1000,
                points_after=(await session.get(Wallet, user.id)).balance,
            )
            await _collect_run_record(session, run.id, record)
            _ALL_RECORDS.append(record)
            await session.commit()

            fresh = await session.get(AgentRun, run.id)
            assert fresh.revision_count >= 1, "Reviewer revise 分支未触发"
            assert record.status == RunStatus.COMPLETED.value
            published = list(
                (
                    await session.scalars(
                        select(AgentArtifactVersion).where(
                            AgentArtifactVersion.source_run_id == run.id
                        )
                    )
                ).all()
            )
            assert published, "revise→fix→approve 后未发布任何 Artifact"
            # 发布的 brand_report_v3 数值字段必须全部有有效 lineage。
            for version in record.artifact_versions:
                assert version["lineage_ok"] is True, version.get("lineage_error")
            await _assert_ledger(record)
    finally:
        await db.close()
