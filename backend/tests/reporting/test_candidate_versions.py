from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.models import KolSnapshot, TaskCandidate
from app.reporting.service import ReportingService
from tests.reporting.fakes import candidate_fixture, completed_task_factory


async def test_same_evidence_reuses_candidate_version(
    db_session: AsyncSession, user_factory
) -> None:
    user = await user_factory()
    task = await completed_task_factory(
        db_session,
        user.id,
        evidence_rows=[candidate_fixture(account_id="100", engagement_rate="8%")],
    )
    service = ReportingService(db_session)

    first = await service.build_candidate_version(task.id, "balanced")
    second = await service.build_candidate_version(task.id, "balanced")

    candidate_count = await db_session.scalar(
        select(func.count()).select_from(TaskCandidate).where(TaskCandidate.task_id == task.id)
    )
    snapshot_count = await db_session.scalar(
        select(func.count())
        .select_from(KolSnapshot)
        .join(TaskCandidate, TaskCandidate.snapshot_id == KolSnapshot.id)
        .where(TaskCandidate.task_id == task.id)
    )
    assert first.candidate_version == second.candidate_version == 1
    assert candidate_count == 1
    assert snapshot_count == 1


async def test_candidate_order_is_stable_across_input_order(
    db_session: AsyncSession, user_factory
) -> None:
    user = await user_factory()
    task = await completed_task_factory(
        db_session,
        user.id,
        evidence_rows=[
            candidate_fixture(
                account_id="100",
                engagement_rate="8%",
                audience_score=80,
                content_score=80,
            ),
            candidate_fixture(
                account_id="200",
                engagement_rate="8%",
                audience_score=90,
                content_score=67.5,
            ),
        ],
    )

    result = await ReportingService(db_session).build_candidate_version(task.id, "balanced")

    assert [(item.platform_account_id, item.rank) for item in result.candidates] == [
        ("200", 1),
        ("100", 2),
    ]


async def test_sensitive_evidence_is_redacted_before_snapshot_and_candidate_storage(
    db_session: AsyncSession, user_factory
) -> None:
    user = await user_factory()
    task = await completed_task_factory(
        db_session,
        user.id,
        evidence_rows=[
            candidate_fixture(
                profile_url="https://datatap.deepminer.com.cn/creator/100",
                risk_flags=[
                    {
                        "authorization": "Bearer should-not-persist",
                        "endpoint": "https://api.lkeap.cloud.tencent.com/plan/v3",
                        "service": "google-trends-mcp",
                        "nested": {"api_key": "secret", "reason": "内容重复"},
                    }
                ],
            )
        ],
    )

    result = await ReportingService(db_session).build_candidate_version(task.id, "balanced")
    snapshot = await db_session.scalar(
        select(KolSnapshot).where(KolSnapshot.id == result.candidates[0].snapshot_id)
    )
    candidate = await db_session.scalar(
        select(TaskCandidate).where(TaskCandidate.snapshot_id == snapshot.id)
    )
    stored = f"{snapshot.normalized_json!r}{candidate.evidence_json!r}".lower()

    for forbidden in (
        "authorization",
        "api_key",
        "endpoint",
        "https://",
        "datatap.deepminer.com.cn",
        "google-trends-mcp",
        "api.lkeap.cloud.tencent.com",
    ):
        assert forbidden not in stored
    assert snapshot.normalized_json["risk_flags"] == [{"nested": {"reason": "内容重复"}}]


async def test_lost_lease_rejects_candidate_artifact_write(
    db_session: AsyncSession, user_factory
) -> None:
    user = await user_factory()
    task = await completed_task_factory(
        db_session, user.id, evidence_rows=[candidate_fixture(account_id="lost-lease")]
    )
    task.lease_owner = "new-worker"
    task.lease_expires_at = task.created_at + timedelta(minutes=5)

    with pytest.raises(RuntimeError, match="task_lease_lost"):
        await ReportingService(db_session).build_candidate_version(
            task.id, "balanced", lease_owner="old-worker"
        )

    assert await db_session.scalar(
        select(func.count()).select_from(TaskCandidate).where(TaskCandidate.task_id == task.id)
    ) == 0


async def test_lost_lease_rejects_bi_artifact_write(
    db_session: AsyncSession, user_factory
) -> None:
    user = await user_factory()
    task = await completed_task_factory(
        db_session, user.id, evidence_rows=[candidate_fixture(account_id="lost-bi")]
    )
    service = ReportingService(db_session)
    await service.build_candidate_version(task.id, "balanced")
    task.lease_owner = "new-worker"
    task.lease_expires_at = task.created_at + timedelta(minutes=5)

    with pytest.raises(RuntimeError, match="task_lease_lost"):
        await service.build_bi_report(task.id, lease_owner="old-worker")
