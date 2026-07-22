from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app
from app.model.contracts import ModelPlanInvalidError, StructuredResult
from app.reporting.blocks import ChartBlock, ChartSeries, MetricGridBlock, MetricItem, ReportDocument
from app.reporting.models import AnalysisReport
from app.selection.analysis import build_kol_analysis_summary
from app.selection.models import SessionKolSelection
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
) -> SessionKolSelection:
    export_fields: dict[str, Any] = {}
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
        fields_json={"export_fields": export_fields},
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
        assert summary["top10"][1]["score_reason"] == ""
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
    service = KolSelectionService(db_session)
    await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
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
async def test_kol_analysis_other_users_session_returns_404(
    analysis_client_factory, db_session: AsyncSession
) -> None:
    _client, user, session_id, _model = await analysis_client_factory()
    await _seed_selection(db_session, user.id, session_id)
    other_client, _other, _other_session, _ = await analysis_client_factory()

    response = await other_client.post(f"/api/v1/sessions/{session_id}/kol-analysis")

    assert response.status_code == 404
