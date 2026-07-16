from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.reporting.analytics import aggregate_analytics, empty_analytics
from app.reporting.models import BiReport, Kol, KolSnapshot, TaskCandidate
from app.reporting.service import ReportingService
from app.reporting.router import bi_report_read
from app.tasks.models import AnalysisTask
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.tasks.state import TaskStatus
from app.workspace.models import WorkspaceSession


def _record(
    platform: str,
    account_id: str,
    fields: dict,
) -> dict:
    return {
        "platform": platform,
        "platform_account_id": account_id,
        "analytics_fields": fields,
    }


def test_aggregate_analytics_returns_deterministic_overview_sentiment_trend_and_audience() -> None:
    records = [
        _record(
            "douyin",
            "dy-1",
            {
                "brand_mentions": 3,
                "exposure": 1_000,
                "interactions": 100,
                "published_at": "2026-07-15",
                "sentiment_counts": {"positive": 8, "neutral": 2},
                "hot_words": [{"term": "测评", "count": 3}, "种草"],
                "audience_age": {"18-24": 60, "25-34": 40},
                "audience_gender": {"女性": 70, "男性": 30},
                "audience_regions": {"浙江": 80, "上海": 20},
            },
        ),
        _record(
            "xiaohongshu",
            "xhs-1",
            {
                "brand_mentions": 2,
                "exposure": 2_000,
                "interactions": 100,
                "published_at": "2026-07-16T10:00:00+08:00",
                "sentiment_counts": {"positive": 5, "neutral": 1, "negative": 4},
                "hot_words": [{"term": "测评", "count": 2}, {"term": "护肤", "count": 5}],
                "audience_age": {"18-24": 20, "25-34": 80},
                "audience_gender": {"女性": 50, "男性": 50},
                "audience_regions": {"浙江": 30, "江苏": 70},
            },
        ),
    ]

    result = aggregate_analytics(records)

    assert result["overview"]["brand_volume"] == {
        "value": 5,
        "unit": "条",
        "available": True,
        "coverage": 1.0,
        "source_fields": ["brand_mentions"],
        "platforms": ["douyin", "xiaohongshu"],
    }
    assert result["overview"]["total_exposure"]["value"] == 3000
    assert result["overview"]["average_engagement_rate"]["value"] == 6.67
    assert result["sentiment"]["available"] is True
    assert result["sentiment"]["items"] == [
        {"key": "positive", "label": "正向", "value": 13, "percentage": 65.0},
        {"key": "neutral", "label": "中立", "value": 3, "percentage": 15.0},
        {"key": "negative", "label": "负向", "value": 4, "percentage": 20.0},
    ]
    assert result["sentiment"]["hot_words"] == [
        {"term": "护肤", "count": 5},
        {"term": "测评", "count": 5},
        {"term": "种草", "count": 1},
    ]
    assert result["exposure_trend"] == [
        {"date": "2026-07-15", "value": 1000, "unit": "次", "platforms": ["douyin"]},
        {"date": "2026-07-16", "value": 2000, "unit": "次", "platforms": ["xiaohongshu"]},
    ]
    assert result["audience"]["age"]["items"] == [
        {"label": "25-34", "value": 60.0, "unit": "%"},
        {"label": "18-24", "value": 40.0, "unit": "%"},
    ]
    assert result["audience"]["regions"]["items"] == [
        {"label": "浙江", "value": 55.0, "unit": "%"},
        {"label": "江苏", "value": 35.0, "unit": "%"},
        {"label": "上海", "value": 10.0, "unit": "%"},
    ]


def test_aggregate_analytics_never_invents_missing_values() -> None:
    result = aggregate_analytics([_record("douyin", "dy-1", {})])

    assert result["overview"]["brand_volume"]["available"] is False
    assert result["overview"]["brand_volume"]["value"] is None
    assert result["overview"]["total_exposure"]["value"] is None
    assert result["overview"]["average_engagement_rate"]["value"] is None
    assert result["sentiment"] == empty_analytics()["sentiment"]
    assert result["exposure_trend"] == []
    assert result["audience"]["age"]["available"] is False


def test_aggregate_analytics_deduplicates_same_evidence_and_sorts_ties_stably() -> None:
    one = _record("douyin", "dy-1", {"exposure": 100, "hot_words": {"乙": 2, "甲": 2}})
    duplicate = _record("douyin", "dy-1", {"exposure": 100, "hot_words": {"乙": 2, "甲": 2}})

    assert aggregate_analytics([one, duplicate]) == aggregate_analytics([duplicate, one])
    result = aggregate_analytics([one, duplicate])
    assert result["overview"]["total_exposure"]["value"] == 100
    assert result["sentiment"]["hot_words"] == [
        {"term": "乙", "count": 2},
        {"term": "甲", "count": 2},
    ]


def test_old_report_is_exposed_with_explicit_empty_analytics() -> None:
    now = datetime(2026, 7, 16, tzinfo=UTC).replace(tzinfo=None)
    report = BiReport(
        id="report-old",
        task_id="task-old",
        session_id="session-old",
        candidate_version=1,
        report_version=1,
        chart_data_json={},
        conclusion_text=None,
        evidence_json={},
        status="completed",
        completed_at=now,
        created_at=now,
        updated_at=now,
    )

    assert bi_report_read(report).analytics == empty_analytics()


@pytest.mark.asyncio
async def test_latest_session_analysis_hides_old_report_when_latest_task_is_running(
    auth_client_factory,
    db_session,
) -> None:
    client = await auth_client_factory("13100000001")
    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "门控品牌",
            "platforms": ["douyin"],
            "category": "美妆",
            "initial_query": "第一轮分析",
        },
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    old_task_id = created.json()["latest_task"]["id"]
    now = datetime.now(UTC).replace(tzinfo=None)
    old_task = await db_session.get(AnalysisTask, old_task_id)
    old_task.status = TaskStatus.COMPLETED.value
    old_task.completed_at = now
    kol_id, snapshot_id = str(uuid4()), str(uuid4())
    db_session.add_all(
        [
            Kol(
                id=kol_id,
                platform="douyin",
                platform_account_id="old-1",
                normalized_profile_url=None,
                created_at=now,
                updated_at=now,
            ),
            KolSnapshot(
                id=snapshot_id,
                kol_id=kol_id,
                source_mcp_call_id=None,
                normalized_json={"nickname": "旧报告达人", "followers": 1000},
                collected_at=now,
                created_at=now,
            ),
            TaskCandidate(
                id=str(uuid4()),
                task_id=old_task_id,
                kol_id=kol_id,
                snapshot_id=snapshot_id,
                candidate_version=1,
                total_score=Decimal("80.000"),
                score_breakdown_json={"dimensions": {}},
                rank=1,
                matched_conditions_json=[],
                risk_flags_json=[],
                recommendation_text="推荐",
                evidence_json={},
                created_at=now,
            ),
            BiReport(
                id=str(uuid4()),
                task_id=old_task_id,
                session_id=session_id,
                candidate_version=1,
                report_version=1,
                chart_data_json={"analytics": {"overview": {"brand_volume": {"value": 9}}}},
                conclusion_text="旧结论",
                evidence_json={},
                status="completed",
                completed_at=now,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.flush()

    workspace = await db_session.get(WorkspaceSession, session_id)
    assert workspace is not None
    await TaskService(db_session).create(
        workspace.user_id,
        session_id,
        TaskCreate(content="第二轮分析"),
    )
    latest = await db_session.scalar(
        select(AnalysisTask)
        .where(AnalysisTask.session_id == session_id)
        .order_by(AnalysisTask.created_at.desc())
    )
    assert latest is not None
    latest.status = TaskStatus.RUNNING.value
    await db_session.flush()

    analysis = await ReportingService(db_session).latest_session_analysis(
        workspace.user_id, session_id
    )
    assert analysis[0].id == latest.id
    assert analysis[1:] == (None, 0, None)

    session = await client.get(f"/api/v1/sessions/{session_id}")
    assert session.status_code == 200
    assert session.json()["latest_task"]["status"] == "running"
    assert session.json()["latest_candidates"] is None
    assert session.json()["latest_report"] is None


@pytest.mark.asyncio
async def test_latest_session_analysis_hides_deleted_session(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13100000002")
    created = await client.post(
        "/api/v1/sessions",
        json={"category": "餐饮", "initial_query": "删除后分析"},
    )
    session_id = created.json()["id"]
    workspace = await db_session.get(WorkspaceSession, session_id)
    assert workspace is not None
    user_id = workspace.user_id
    assert (await client.delete(f"/api/v1/sessions/{session_id}")).status_code == 204

    result = await ReportingService(db_session).latest_session_analysis(user_id, session_id)
    assert result == (None, None, 0, None)
