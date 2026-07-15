from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.reporting.schemas import DimensionInputs, ToolEvidence
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


def evidence(
    *,
    platform: str = "bilibili",
    account_id: str = "100",
    nickname: str = "测试达人",
    source_call_id: str | None = None,
    **metrics: Any,
) -> ToolEvidence:
    payload = {
        "platform": platform,
        "account_id": account_id,
        "nickname": nickname,
        "followers": metrics.pop("followers", "1.2万"),
        "engagement_rate": metrics.pop("engagement_rate", None),
        "engagement_score": metrics.pop("engagement_score", 80),
        "content_score": metrics.pop("content_score", 80),
        "audience_score": metrics.pop("audience_score", 80),
        "budget_score": metrics.pop("budget_score", 80),
        "growth_score": metrics.pop("growth_score", 80),
        "brand_safety_score": metrics.pop("brand_safety_score", 80),
        **metrics,
    }
    return ToolEvidence(
        internal_tool_name="creator.search.v1",
        payload=payload,
        source_call_id=source_call_id,
        collected_at=datetime.now(UTC).replace(tzinfo=None),
    )


def all_dimensions(score: float | None, **overrides: float | None) -> DimensionInputs:
    return DimensionInputs(
        audience=overrides.get("audience", score),
        content=overrides.get("content", score),
        engagement=overrides.get("engagement", score),
        budget=overrides.get("budget", score),
        growth=overrides.get("growth", score),
        brand_safety=overrides.get("brand_safety", score),
    )


def candidate_fixture(**overrides: Any) -> ToolEvidence:
    return evidence(**overrides)


def report_fixture(**overrides: Any) -> dict[str, Any]:
    return {"profile": "balanced", "conclusion": "测试结论", **overrides}


async def completed_task_factory(
    db: AsyncSession,
    user_id: str,
    *,
    evidence_rows: list[ToolEvidence],
) -> AnalysisTask:
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user_id,
        title="候选计算",
        brand="测试品牌",
        campaign_name="候选排序",
        status="active",
        platforms=["bilibili"],
        category="美妆",
        target_audience="测试受众",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    message = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        role="user",
        content="找达人",
        sequence=1,
        metadata_json={"scoring_profile": "balanced"},
        created_at=now,
    )
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session.id,
        trigger_message_id=message.id,
        status="running",
        plan_json={},
        plan_version="planner_v1",
        max_calls=10,
        estimated_points=10,
        error_code=None,
        error_message=None,
        cancel_requested_at=None,
        lease_owner="test-worker",
        lease_expires_at=None,
        started_at=now,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    # 这些模型没有声明 ORM relationship；按外键依赖顺序持久化。
    db.add(session)
    await db.flush()
    db.add(message)
    await db.flush()
    db.add(task)
    await db.flush()
    for index, item in enumerate(evidence_rows, start=1):
        call = McpCall(
            id=item.source_call_id or str(uuid4()),
            logical_call_id=str(uuid4()),
            task_id=task.id,
            batch_no=0,
            plan_step_id=f"step-{index}",
            attempt=1,
            service_slug="bilibili-mcp",
            internal_tool_name=item.internal_tool_name,
            arguments_digest="a" * 64,
            status=McpCallStatus.SETTLED.value,
            reservation_transaction_id=None,
            settlement_transaction_id=None,
            upstream_request_id=f"request-{index}",
            protocol_session_digest=None,
            response_hash="b" * 64,
            evidence_json={
                "outcome": "succeeded",
                "structured_content": item.payload,
                "upstream_request_id": f"request-{index}",
            },
            error_type=None,
            error_message=None,
            duration_ms=None,
            started_at=now,
            completed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(call)
    await db.flush()
    return task
