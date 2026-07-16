from types import SimpleNamespace

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.reporting.models import BiReport, Kol, KolSnapshot, TaskCandidate
from app.tasks.models import AnalysisTask

from app.reporting.service import ReportingService


def _candidate(index: int):
    platform = "xiaohongshu" if index % 2 == 0 else "douyin"
    kol = SimpleNamespace(platform_account_id=f"account-{index}")
    row = SimpleNamespace(platform=platform)
    score = SimpleNamespace(
        total=100 - index,
        dimensions={
            "audience": SimpleNamespace(raw_score=80 - index),
            "engagement": SimpleNamespace(raw_score=70 - index),
        },
    )
    return kol, None, row, score


def test_combined_platform_pool_is_ranked_then_limited_to_final_top10() -> None:
    selector = getattr(ReportingService, "_select_top_candidates", None)

    assert callable(selector)
    selected = selector([_candidate(index) for index in range(12)])

    assert len(selected) == 10
    assert [item[3].total for item in selected] == list(range(100, 90, -1))
    assert {item[2].platform for item in selected} == {"xiaohongshu", "douyin"}


@pytest.mark.asyncio
async def test_deleted_session_hides_candidates_and_reports(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000031")
    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "报告隔离品牌",
            "campaign_name": "软删除",
            "platforms": ["xiaohongshu"],
            "category": "美妆",
            "target_audience": "年轻女性",
            "initial_query": "创建候选任务",
        },
    )
    session_id = created.json()["id"]
    task_id = created.json()["latest_task"]["id"]
    now = datetime.now(UTC).replace(tzinfo=None)
    kol_id = str(uuid4())
    snapshot_id = str(uuid4())
    report_id = str(uuid4())
    db_session.add_all(
        [
            Kol(
                id=kol_id,
                platform="xiaohongshu",
                platform_account_id=f"account-{kol_id}",
                normalized_profile_url=None,
                created_at=now,
                updated_at=now,
            ),
            KolSnapshot(
                id=snapshot_id,
                kol_id=kol_id,
                source_mcp_call_id=None,
                normalized_json={"nickname": "隔离达人", "followers": 1000},
                collected_at=now,
                created_at=now,
            ),
            TaskCandidate(
                id=str(uuid4()),
                task_id=task_id,
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
                id=report_id,
                task_id=task_id,
                session_id=session_id,
                candidate_version=1,
                report_version=1,
                chart_data_json={},
                conclusion_text="结论",
                evidence_json={},
                status="completed",
                completed_at=now,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    task = await db_session.get(AnalysisTask, task_id)
    task.status = "completed"
    task.completed_at = now
    await db_session.flush()

    assert (await client.delete(f"/api/v1/sessions/{session_id}")).status_code == 204
    assert (await client.get(f"/api/v1/tasks/{task_id}/candidates")).status_code == 404
    assert (await client.get(f"/api/v1/reports/{report_id}")).status_code == 404
