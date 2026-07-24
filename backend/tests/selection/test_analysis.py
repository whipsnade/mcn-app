from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import TaskArtifact
from app.artifacts.service import ArtifactService
from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app
from app.model.contracts import ModelPlanInvalidError, StructuredResult
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import ChartBlock, ChartSeries, MetricGridBlock, MetricItem, ReportDocument
from app.reporting.models import AnalysisReport
from app.selection.analysis import build_kol_analysis_summary
from app.selection.models import KolSelectionItem, SessionKolSelection
from app.selection.router import kol_analysis_model
from app.selection.service import KolSelectionService
from app.workspace.models import WorkspaceSession


def _row(
    uid: str,
    *,
    platform: str = "xiaohongshu",
    nickname: str = "达人",
    followers: int | None = 125000,
    city: str | None = "杭州市",
    total: float = 80.0,
    rating: str = "重点推荐",
    score_reason: str | None = None,
    extra_export_fields: dict[str, Any] | None = None,
    engagement_rate: Any = None,
    missing_fields: list[str] | None = None,
) -> SessionKolSelection:
    export_fields: dict[str, Any] = dict(extra_export_fields or {})
    if score_reason is not None:
        export_fields["score_reason"] = score_reason
    now = datetime.now(UTC).replace(tzinfo=None)
    return SessionKolSelection(
        id=str(uuid4()),
        user_id="u1",
        session_id="s1",
        platform=platform,
        kol_uid=uid,
        nickname=nickname,
        followers=followers,
        city=city,
        profile_url=None,
        fields_json={
            "export_fields": export_fields,
            "engagement_rate": engagement_rate,
            "missing_fields": list(missing_fields or []),
        },
        score_json={"total": total, "rating": rating, "stars": "★★★★★", "dimensions": {}},
        source_tool="tool",
        first_task_id="t1",
        last_task_id="t1",
        created_at=now,
        updated_at=now,
    )


class TestBuildKolAnalysisSummary:
    def test_aggregates_all_buckets(self) -> None:
        rows = [
            _row("a", total=90.0, rating="重点推荐", followers=6_000_000, city="杭州市",
                 score_reason="受众匹配"),
            _row("b", platform="douyin", total=70.0, rating="推荐", followers=700_000,
                 city="杭州市"),
            _row("c", platform="douyin", total=50.0, rating="可考虑", followers=200_000,
                 city="上海市"),
            _row("d", platform="weibo", total=30.0, rating="观察", followers=50_000,
                 city=None),
        ]

        summary = build_kol_analysis_summary(
            rows, brand="海底捞", category="美食", target_audience="25-35岁"
        )

        assert summary["total"] == 4
        assert summary["platform_counts"] == {"小红书": 1, "抖音": 2, "微博": 1}
        assert summary["rating_counts"] == {
            "重点推荐": 1, "推荐": 1, "可考虑": 1, "观察": 1,
        }
        assert summary["followers_buckets"] == {
            "<10万": 1, "10-50万": 1, "50-100万": 1, "100-500万": 0, ">500万": 1,
        }
        assert summary["avg_score"] == 60.0
        assert summary["city_top10"] == [
            {"city": "杭州市", "count": 2},
            {"city": "上海市", "count": 1},
        ]
        assert [item["nickname"] for item in summary["top10"]] == [
            row.nickname for row in rows
        ]
        first = summary["top10"][0]
        assert first == {
            "nickname": "达人",
            "platform": "小红书",
            "followers": 6_000_000,
            "total_score": 90.0,
            "rating": "重点推荐",
            "score_reason": "受众匹配",
        }
        assert summary["top10"][1]["score_reason"] == "基于规范化 MCP 数据评分"
        assert summary["brand"] == "海底捞"
        assert summary["category"] == "美食"
        assert summary["target_audience"] == "25-35岁"

    def test_fixed_bucket_keys_present_when_empty(self) -> None:
        rows = [
            _row("a", total=65.0, rating="推荐", followers=None, city=None),
        ]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["rating_counts"] == {
            "重点推荐": 0, "推荐": 1, "可考虑": 0, "观察": 0,
        }
        assert summary["followers_buckets"] == {
            "<10万": 0, "10-50万": 0, "50-100万": 0, "100-500万": 0, ">500万": 0,
        }
        assert summary["avg_score"] == 65.0
        assert summary["city_top10"] == []
        assert summary["top10"][0]["followers"] is None
        assert summary["category"] is None

    def test_avg_score_zero_without_scores(self) -> None:
        row = _row("a", total=0.0, rating="观察")
        row.score_json = {}

        summary = build_kol_analysis_summary([row], brand="", category=None, target_audience="")

        assert summary["avg_score"] == 0
        assert summary["rating_counts"] == {
            "重点推荐": 0, "推荐": 0, "可考虑": 0, "观察": 0,
        }

    def test_top10_truncates_to_ten(self) -> None:
        rows = [_row(f"k{i}", total=float(100 - i)) for i in range(12)]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert len(summary["top10"]) == 10
        assert summary["top10"][0]["total_score"] == 100.0
        assert summary["top10"][-1]["total_score"] == 91.0

    def test_unknown_platform_code_kept_as_is(self) -> None:
        rows = [_row("a", platform="kuaishou")]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["platform_counts"] == {"kuaishou": 1}
        assert summary["top10"][0]["platform"] == "kuaishou"

    def test_followers_bucket_boundaries_belong_to_upper_bucket(self) -> None:
        rows = [
            _row("a", followers=99_999),
            _row("b", followers=100_000),
            _row("c", followers=499_999),
            _row("d", followers=500_000),
            _row("e", followers=999_999),
            _row("f", followers=1_000_000),
            _row("g", followers=4_999_999),
            _row("h", followers=5_000_000),
        ]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["followers_buckets"] == {
            "<10万": 1, "10-50万": 2, "50-100万": 2, "100-500万": 2, ">500万": 1,
        }

    def test_score_reason_falls_back_to_chinese_key(self) -> None:
        rows = [_row("a", extra_export_fields={"评分理由": "受众高度契合"})]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["top10"][0]["score_reason"] == "受众高度契合"

    def test_score_reason_truncated_to_200_chars(self) -> None:
        rows = [_row("a", score_reason="长" * 300)]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["top10"][0]["score_reason"] == "长" * 200

    def test_score_reason_generated_from_missing_fields_matches_export_rule(self) -> None:
        """export_fields 无 score_reason 时按与 Excel 导出相同的规则生成。"""
        rows = [
            _row("a", missing_fields=["engagement_rate", "quoted_price_cny"]),
            _row("b"),
        ]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["top10"][0]["score_reason"] == "数据缺失字段按评分规则处理"
        assert summary["top10"][1]["score_reason"] == "基于规范化 MCP 数据评分"

    def test_engagement_rate_buckets_parse_numeric_and_string(self) -> None:
        rows = [
            _row("a", engagement_rate=2.5),
            _row("b", engagement_rate="3.0%"),
            _row("c", engagement_rate=7.5),
            _row("d", engagement_rate="12%"),
            _row("e", engagement_rate=None),
            _row("f", engagement_rate="无法解析"),
        ]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["engagement_rate_buckets"] == {
            "<3%": 1, "3-5%": 1, "5-10%": 1, ">10%": 1, "未知": 2,
        }

    def test_engagement_rate_bucket_boundaries(self) -> None:
        rows = [
            _row("a", engagement_rate=3.0),
            _row("b", engagement_rate=5.0),
            _row("c", engagement_rate=10.0),
        ]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["engagement_rate_buckets"] == {
            "<3%": 0, "3-5%": 1, "5-10%": 2, ">10%": 0, "未知": 0,
        }

    def test_engagement_rate_buckets_fixed_keys_when_empty(self) -> None:
        rows = [_row("a", engagement_rate=None)]

        summary = build_kol_analysis_summary(rows, brand="", category=None, target_audience="")

        assert summary["engagement_rate_buckets"] == {
            "<3%": 0, "3-5%": 0, "5-10%": 0, ">10%": 0, "未知": 1,
        }


_XHS_TOOL = "datatap.xiaohongshu.kol.search.v1"


def _xhs_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"result": json.dumps({"KOL 列表": list(rows)}, ensure_ascii=False)}


def _xhs_row(uid: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "账号ID (kwUid)": uid,
        "昵称": f"达人{uid}",
        "粉丝数": "12.5万",
        "互动率-图文笔记": "5.2%",
        "综合评分": 88,
        "有效粉丝率": "65%",
        "城市": "杭州市",
    }
    row.update(overrides)
    return row


def _document() -> ReportDocument:
    return ReportDocument(
        title="KOL 圈选分析",
        conclusion="名单质量良好。",
        blocks=[
            MetricGridBlock(
                items=[MetricItem(label="圈选总数", value=2)],
            ),
            ChartBlock(
                type="bar_chart",
                categories=["重点推荐", "推荐"],
                series=[ChartSeries(name="人数", values=[1, 1])],
            ),
        ],
    )


class FakeModel:
    def __init__(self, document: ReportDocument | None) -> None:
        self.document = document
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        if self.document is None:
            raise ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False)
        return StructuredResult(
            value=self.document,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )

    def stream_text(self, request):  # pragma: no cover - 本端点不走流式
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def analysis_client_factory(db_session: AsyncSession):
    clients: list[AsyncClient] = []

    async def create(
        *, valid_model: bool = True,
    ) -> tuple[AsyncClient, User, str, FakeModel]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        model = FakeModel(_document() if valid_model else None)
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[kol_analysis_model] = lambda: model
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname="分析用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        session = WorkspaceSession(
            id=str(uuid4()),
            user_id=user.id,
            title="圈选会话",
            brand="海底捞",
            campaign_name=None,
            status="active",
            platforms=["xiaohongshu"],
            category="美食",
            target_audience="25-35岁",
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
        return test_client, user, session.id, model

    yield create
    for test_client in clients:
        await test_client.aclose()


async def _seed_selection(db_session: AsyncSession, user_id: str, session_id: str) -> None:
    """按现行写入路径播种：先建 selection set，再往 items 里沉淀（端点已切读新表）。"""
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="默认名单"
    )
    await service.ingest_tool_evidence_to_set(
        user_id=user_id,
        selection_set_id=selection_set.id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(_xhs_row("xhs-001"), _xhs_row("xhs-002")),
    )


@pytest.mark.asyncio
async def test_kol_analysis_empty_selection_returns_409(analysis_client_factory) -> None:
    client, _user, session_id, _model = await analysis_client_factory()

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 409
    assert response.json()["detail"] == "NO_KOL_SELECTION"


@pytest.mark.asyncio
async def test_kol_analysis_builds_session_report(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    client, user, session_id, model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "KOL 圈选分析"
    assert body["conclusion"] == "名单质量良好。"
    assert body["version"] == 1
    assert [block["type"] for block in body["blocks"]] == ["metric_grid", "bar_chart"]
    [request] = model.requests
    assert request.purpose == "kol_analysis"
    assert request.template_name == "kol_analysis_v1"
    payload = json.loads(request.messages[-1].content)
    assert payload["total"] == 2
    assert payload["brand"] == "海底捞"
    assert payload["category"] == "美食"
    assert payload["target_audience"] == "25-35岁"
    assert payload["top10"][0]["nickname"] == "达人xhs-001"

    # 再次触发：会话内版本递增。
    second = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")
    assert second.status_code == 200
    assert second.json()["version"] == 2
    reports = list(
        (
            await db_session.scalars(
                select(AnalysisReport).where(AnalysisReport.session_id == session_id)
            )
        ).all()
    )
    assert sorted(report.version for report in reports) == [1, 2]
    assert all(report.task_id is None for report in reports)


@pytest.mark.asyncio
async def test_kol_analysis_invalid_model_output_returns_502(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    client, user, session_id, _model = await analysis_client_factory(valid_model=False)
    await _seed_selection(db_session, user.id, session_id)

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 502
    assert (
        await db_session.scalar(
            select(AnalysisReport).where(AnalysisReport.session_id == session_id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_kol_analysis_version_conflict_returns_409(
    analysis_client_factory, db_session: AsyncSession, monkeypatch
) -> None:
    client, user, session_id, _model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)

    async def conflict(self, **kwargs):
        raise LookupError("report_version_conflict")

    monkeypatch.setattr(AnalysisReportService, "build_session_report", conflict)

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 409
    assert response.json()["detail"] == "REPORT_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_kol_analysis_report_carries_scope_snapshot(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    """kol-analysis 报告落 report_type=kol_analysis 与 brand/category scope 快照。"""
    client, user, session_id, _model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 200
    report = await db_session.scalar(
        select(AnalysisReport).where(AnalysisReport.session_id == session_id)
    )
    assert report is not None
    assert report.report_type == "kol_analysis"
    assert report.scope_json == {"brand": "海底捞", "category": "美食"}


@pytest.mark.asyncio
async def test_kol_analysis_registers_manual_artifact(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    """手动分析成功后登记 manual Artifact；同 report_id 重复登记幂等。"""
    client, user, session_id, _model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 200
    report_id = response.json()["id"]
    artifact = await db_session.scalar(
        select(TaskArtifact).where(
            TaskArtifact.artifact_key == f"manual:{report_id}:kol_report"
        )
    )
    assert artifact is not None
    assert artifact.artifact_type == "kol_report"
    assert artifact.report_id == report_id
    assert artifact.task_id is None
    assert artifact.status == "completed"
    assert artifact.scope_json == {"brand": "海底捞", "category": "美食"}

    # 恢复重放同一报告的登记：artifact_key 幂等，不重复建行。
    await ArtifactService(db_session).register_artifact(
        user_id=user.id,
        session_id=session_id,
        artifact_key=f"manual:{report_id}:kol_report",
        artifact_type="kol_report",
        title=artifact.title,
        version=artifact.version,
        report_id=report_id,
    )
    total = await db_session.scalar(
        select(func.count()).select_from(TaskArtifact).where(
            TaskArtifact.session_id == session_id
        )
    )
    assert total == 1


@pytest.mark.asyncio
async def test_kol_analysis_works_with_backfilled_set_only(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    """只有回填数据（历史默认名单 set）的会话可正常手动分析。"""
    client, user, session_id, _model = await analysis_client_factory()
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user.id, session_id, title="历史默认名单"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        KolSelectionItem(
            id=str(uuid4()),
            user_id=user.id,
            selection_set_id=selection_set.id,
            platform="xiaohongshu",
            kol_uid="uid-legacy",
            nickname="回填达人",
            followers=1000,
            city="杭州市",
            profile_url=None,
            fields_json={"export_fields": {}},
            score_json={"total": 80.0, "rating": "重点推荐", "stars": "★★★★★", "dimensions": {}},
            source_tool="tool",
            first_task_id="t1",
            last_task_id="t1",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    response = await client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 200
    assert response.json()["version"] == 1


@pytest.mark.asyncio
async def test_kol_analysis_other_users_session_returns_404(    analysis_client_factory, db_session: AsyncSession
) -> None:
    _client, user, session_id, _model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)
    other_client, _other, _other_session, _ = await analysis_client_factory()

    response = await other_client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 404
