"""POST /sessions/{id}/analysis-retry：品牌/活动报告手动重试（零积分、不调 MCP）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import TaskArtifact
from app.billing.models import Wallet
from app.core.security import create_access_token
from app.db.session import get_db
from app.goals.models import TaskGoal
from app.identity.models import LoginSession, User
from app.main import create_app
from app.model.contracts import ModelPlanInvalidError, StructuredResult
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.reporting.brand_payload import BrandReportNarrative, BrandReportPayload
from app.reporting.models import AnalysisReport
from app.reporting.router import analysis_model
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


def _document() -> ReportDocument:
    return ReportDocument(
        title="品牌声量分析",
        conclusion="品牌声量稳步上升。",
        blocks=[MetricGridBlock(items=[MetricItem(label="总声量", value=1200)])],
    )


_NARRATIVE_OUTPUT = {
    "praise_points": ["新品测评内容互动表现好"],
    "complaint_points": [],
    "impact_level": "低",
    "expansion_signals": [],
    "noise_notes": None,
    "key_findings": ["正面声量占主导"],
    "conclusion": "品牌声量稳步上升。",
    "recommendations": ["延续新品测评内容节奏"],
}


class FakeModel:
    """按 request.output_model 分派：品牌 v2 叙事输出 narrative dict，其余输出 document。"""

    def __init__(self, document: ReportDocument | None) -> None:
        self.document = document

    async def complete_json(self, request):
        if self.document is None:
            raise ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False)
        raw = (
            _NARRATIVE_OUTPUT
            if request.output_model is BrandReportNarrative
            else self.document.model_dump(mode="json")
        )
        value = request.output_model.model_validate(raw)
        return StructuredResult(
            value=value,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )


@pytest_asyncio.fixture
async def retry_client_factory(db_session: AsyncSession):
    clients: list[AsyncClient] = []

    async def create(*, valid_model: bool = True) -> tuple[AsyncClient, User, str]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        model = FakeModel(_document() if valid_model else None)
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[analysis_model] = lambda: model
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname="重试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            Wallet(user_id=user.id, balance=1000, reserved=0, version=1, updated_at=now)
        )
        session = WorkspaceSession(
            id=str(uuid4()),
            user_id=user.id,
            title="重试会话",
            brand="海底捞",
            campaign_name=None,
            status="active",
            platforms=["xiaohongshu"],
            category="美食",
            target_audience="",
            budget_min=None,
            budget_max=None,
            filters_snapshot={},
            is_starred=False,
            last_accessed_at=now,
            created_at=now,
            updated_at=now,
        )
        db_session.add(session)
        login_session = LoginSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=uuid4().hex + uuid4().hex,
            expires_at=now + timedelta(days=1),
            revoked_at=None,
            created_at=now,
            last_seen_at=now,
        )
        db_session.add(login_session)
        await db_session.flush()
        token = create_access_token(user_id=user.id, session_id=login_session.id, role="user")
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        test_client.headers["Authorization"] = f"Bearer {token}"
        clients.append(test_client)
        return test_client, user, session.id

    yield create
    for test_client in clients:
        await test_client.aclose()


async def _seed_goal(
    db_session: AsyncSession,
    user_id: str,
    session_id: str,
    *,
    goal_type: str = "brand_analysis",
    with_evidence: bool = True,
    plan_results: list[dict] | None = None,
) -> TaskGoal:
    now = datetime.now(UTC).replace(tzinfo=None)
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="user",
        content="分析品牌声量",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    if plan_results is None:
        plan_results = (
            [
                {
                    "step_id": "step_1",
                    "tool": "datatap.insight.social.statistic.overview.v1",
                    "status": "settled",
                    "summary": {
                        "result": json.dumps(
                            [{"平台": "小红书", "声量": 12345, "曝光量": 50000, "互动数": 8000}],
                            ensure_ascii=False,
                        )
                    },
                }
            ]
            if with_evidence
            else []
        )
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session_id,
        trigger_message_id=message.id,
        status="completed",
        kind="agent",
        plan_json={"schema": "agent_trajectory_v1", "steps": [], "results": plan_results},
        max_calls=10,
        estimated_points=0,
        creation_order=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(task)
    await db_session.flush()
    goal = TaskGoal(
        id=str(uuid4()),
        task_id=task.id,
        sequence=1,
        goal_type=goal_type,
        status="completed",
        params_json={"brand": "海底捞", "campaign": "618大促" if goal_type == "campaign_analysis" else None},
        created_at=now,
        updated_at=now,
    )
    db_session.add(goal)
    await db_session.flush()
    return goal


@pytest.mark.asyncio
async def test_analysis_retry_builds_new_version_without_points(
    retry_client_factory, db_session: AsyncSession
) -> None:
    client, user, session_id = await retry_client_factory()
    await _seed_goal(db_session, user.id, session_id)

    first = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert first.status_code == 200
    assert first.json()["version"] == 1
    assert first.json()["title"] == "海底捞 品牌分析报告"

    second = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert second.status_code == 200
    assert second.json()["version"] == 2

    # 零积分：钱包余额不变。
    wallet = await db_session.get(Wallet, user.id)
    assert wallet is not None
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    # manual artifact 按 report_id 登记。
    artifact = await db_session.scalar(
        select(TaskArtifact).where(
            TaskArtifact.artifact_key == f"manual:{second.json()['id']}:brand_report"
        )
    )
    assert artifact is not None
    assert artifact.artifact_type == "brand_report"
    assert artifact.report_id == second.json()["id"]
    assert artifact.version == 2


@pytest.mark.asyncio
async def test_analysis_retry_brand_versions_carry_v2_payload(
    retry_client_factory, db_session: AsyncSession
) -> None:
    """品牌重试：新版本带 payload/template_version 落库，旧版本行不被改写。"""
    client, user, session_id = await retry_client_factory()
    await _seed_goal(db_session, user.id, session_id)

    first = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert first.status_code == 200
    first_id = first.json()["id"]
    before = await db_session.get(AnalysisReport, first_id)
    assert before is not None
    first_payload = before.payload_json
    first_updated_at = before.updated_at

    second = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert second.status_code == 200

    rows = list(
        (
            await db_session.scalars(
                select(AnalysisReport)
                .where(
                    AnalysisReport.session_id == session_id,
                    AnalysisReport.report_type == "brand_analysis",
                )
                .order_by(AnalysisReport.version)
            )
        ).all()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.template_version == "brand_report_v2"
        payload = BrandReportPayload.model_validate(row.payload_json)
        assert payload.narrative is not None
        assert row.conclusion_text == payload.narrative.conclusion
        assert row.blocks_json
    # 旧版本行不被改写。
    v1 = rows[0]
    assert v1.id == first_id
    assert v1.payload_json == first_payload
    assert v1.updated_at == first_updated_at
    assert rows[1].id == second.json()["id"]


@pytest.mark.asyncio
async def test_analysis_retry_campaign_type(retry_client_factory, db_session: AsyncSession) -> None:
    client, user, session_id = await retry_client_factory()
    await _seed_goal(db_session, user.id, session_id, goal_type="campaign_analysis")

    response = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "campaign_analysis"},
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1
    artifact = await db_session.scalar(
        select(TaskArtifact).where(
            TaskArtifact.artifact_key == f"manual:{response.json()['id']}:campaign_report"
        )
    )
    assert artifact is not None
    assert artifact.artifact_type == "campaign_report"


@pytest.mark.asyncio
async def test_analysis_retry_without_evidence_returns_409(
    retry_client_factory, db_session: AsyncSession
) -> None:
    client, user, session_id = await retry_client_factory()

    # 无 goal：409。
    response = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "NO_EVIDENCE"

    # 有 goal 但证据为空：同样 409。
    await _seed_goal(db_session, user.id, session_id, with_evidence=False)
    empty = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )
    assert empty.status_code == 409
    assert empty.json()["detail"] == "NO_EVIDENCE"


@pytest.mark.asyncio
async def test_analysis_retry_v2_trajectory_uses_brand_goal_slice(
    retry_client_factory, db_session: AsyncSession
) -> None:
    """v2 多 goal 轨迹：retry 预检与构建都按品牌 goal 切片取证据（200 且不被 kol 切片污染）。"""
    client, user, session_id = await retry_client_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user.id,
        role="user",
        content="分析品牌并圈选达人",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    brand_goal_id = str(uuid4())
    kol_goal_id = str(uuid4())
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session_id,
        trigger_message_id=message.id,
        status="completed",
        kind="agent",
        plan_json={
            "schema": "agent_trajectory_v2",
            "goals": {
                brand_goal_id: {
                    "steps": [],
                    "results": [
                        {
                            "step_id": "g1_step_1",
                            "tool": "datatap.insight.social.statistic.overview.v1",
                            "status": "settled",
                            "summary": {
                                "result": json.dumps(
                                    [{"平台": "小红书", "声量": 12345, "互动数": 8000}],
                                    ensure_ascii=False,
                                )
                            },
                        },
                        {
                            "step_id": "g1_step_2",
                            "tool": "datatap.insight.query.raw.posts.v1",
                            "status": "settled",
                            "summary": {
                                "result": json.dumps(
                                    [{"平台": "小红书", "帖子ID": "x1", "标题": "品牌切片热帖", "互动数": 500}],
                                    ensure_ascii=False,
                                )
                            },
                        },
                    ],
                },
                kol_goal_id: {
                    "steps": [],
                    "results": [
                        {
                            "step_id": "g2_step_1",
                            "tool": "datatap.insight.query.raw.posts.v1",
                            "status": "settled",
                            "summary": {
                                "result": json.dumps(
                                    [{"平台": "抖音", "作品ID": "k1", "标题": "KOL污染帖", "互动数": 9999}],
                                    ensure_ascii=False,
                                )
                            },
                        },
                        {
                            "step_id": "g2_step_2",
                            "tool": "datatap.insight.match.best.tag.v1",
                            "status": "settled",
                            "summary": {"result": json.dumps([{"标签名称": "海底捞"}], ensure_ascii=False)},
                        },
                    ],
                },
            },
        },
        max_calls=10,
        estimated_points=0,
        creation_order=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskGoal(
            id=brand_goal_id,
            task_id=task.id,
            sequence=1,
            goal_type="brand_analysis",
            status="completed",
            params_json={"brand": "海底捞"},
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        TaskGoal(
            id=kol_goal_id,
            task_id=task.id,
            sequence=2,
            goal_type="kol_selection",
            status="completed",
            params_json={"brand": "海底捞"},
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    response = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )

    assert response.status_code == 200
    row = await db_session.get(AnalysisReport, response.json()["id"])
    assert row is not None
    payload = BrandReportPayload.model_validate(row.payload_json)
    # 热帖只来自品牌切片；kol 切片的标签匹配不得污染 query_spec。
    assert [post.title for post in payload.data.top_posts] == ["品牌切片热帖"]
    assert payload.query_spec.matched_tag is None
    assert payload.query_spec.fallback_keyword == "海底捞"
    assert payload.data.overview.total_mentions.current == 12345.0


@pytest.mark.asyncio
async def test_analysis_retry_legacy_evidence_without_overview_returns_409(
    retry_client_factory, db_session: AsyncSession
) -> None:
    """旧轨迹只有非 overview 证据：预检通过但组装器门禁拒绝 → 409 NO_EVIDENCE（非 500）。"""
    client, user, session_id = await retry_client_factory()
    await _seed_goal(
        db_session,
        user.id,
        session_id,
        plan_results=[
            {
                "step_id": "step_1",
                "tool": "datatap.insight.query.analysis.v1",
                "status": "settled",
                "summary": {"total_volume": 12345},
            }
        ],
    )

    response = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "NO_EVIDENCE"


@pytest.mark.asyncio
async def test_analysis_retry_foreign_session_returns_404(
    retry_client_factory, db_session: AsyncSession
) -> None:
    _owner_client, owner, session_id = await retry_client_factory()
    await _seed_goal(db_session, owner.id, session_id)
    other_client, _other, _other_session = await retry_client_factory()

    response = await other_client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_analysis_retry_model_error_returns_502(
    retry_client_factory, db_session: AsyncSession
) -> None:
    client, user, session_id = await retry_client_factory(valid_model=False)
    await _seed_goal(db_session, user.id, session_id)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/analysis-retry",
        json={"report_type": "brand_analysis"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "ANALYSIS_MODEL_ERROR"
