from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.orchestration.loop import AgentTrajectory
from app.selection.models import KolSelectionItem
from app.selection.service import KolSelectionService
from app.selection.top10_enrichment import (
    DetailEnrichmentPlan,
    group_detail_targets_by_platform,
    select_top20_detail_targets,
)
from app.tasks.executor import TaskExecutor

from .test_selection_sets import _create_session


def _item(platform: str, uid: str, average_interactions: object) -> SimpleNamespace:
    return SimpleNamespace(
        platform=platform,
        kol_uid=uid,
        fields_json={"export_fields": {"average_interactions": average_interactions}},
    )


def test_selects_top20_across_platforms_by_average_interactions() -> None:
    rows = [_item("xiaohongshu", f"xhs-{index}", index * 100) for index in range(1, 22)]
    rows.extend([_item("douyin", "dy-1", "2500"), _item("weibo", "wb-1", "bad")])

    targets = select_top20_detail_targets(rows)

    assert len(targets) == 20
    assert targets[0].platform == "douyin"
    assert targets[0].kol_uid == "dy-1"
    assert targets[0].rank == 1
    assert targets[0].ranking_interaction == 2500.0
    assert targets[-1].rank == 20
    assert "wb-1" not in {target.kol_uid for target in targets}


def test_groups_top20_targets_into_one_batch_request_per_platform() -> None:
    targets = select_top20_detail_targets(
        [_item("douyin", "dy-1", 300), _item("xiaohongshu", "xhs-1", 200), _item("douyin", "dy-2", 100)]
    )

    grouped = group_detail_targets_by_platform(targets)

    assert [(platform, [item.kol_uid for item in items]) for platform, items in grouped] == [
        ("douyin", ["dy-1", "dy-2"]),
        ("xiaohongshu", ["xhs-1"]),
    ]


@pytest.mark.asyncio
async def test_plan_uses_the_current_task_selection_set(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-current", goal_id="goal-1", title="名单"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add_all(
        [
            KolSelectionItem(
                id=str(uuid4()), user_id=user_id, selection_set_id=selection_set.id,
                platform="douyin", kol_uid="dy-1", nickname="抖音达人", followers=None,
                city=None, profile_url=None,
                fields_json={"export_fields": {"average_interactions": 500}}, score_json={},
                source_tool="tool", first_task_id="task-current", last_task_id="task-current",
                created_at=now, updated_at=now,
            ),
            KolSelectionItem(
                id=str(uuid4()), user_id=user_id, selection_set_id=selection_set.id,
                platform="xiaohongshu", kol_uid="xhs-1", nickname="小红书达人", followers=None,
                city=None, profile_url=None,
                fields_json={"export_fields": {"average_interactions": 800}}, score_json={},
                source_tool="tool", first_task_id="task-current", last_task_id="task-current",
                created_at=now, updated_at=now,
            ),
        ]
    )
    await db_session.flush()

    plan = await service.build_top20_detail_plan(
        user_id=user_id, session_id=session_id, task_id="task-current", goal_id="goal-1"
    )

    assert plan is not None
    assert plan.selection_set_id == selection_set.id
    assert plan.groups[0][0] == "douyin"
    assert plan.groups[1][0] == "xiaohongshu"
    assert plan.groups[1][1][0].rank == 1


class _FakeGateway:
    def __init__(self) -> None:
        self.commands = ()

    async def execute_batch(self, commands):
        self.commands = commands
        return tuple(
            SimpleNamespace(status="settled", evidence_json={"structured_content": {"result": "{}"}})
            for _command in commands
        )


class _FakeDetailEnrichment:
    def __init__(self, plan: DetailEnrichmentPlan) -> None:
        self.plan = plan
        self.persisted = []

    async def prepare(self, **_kwargs):
        return self.plan

    async def persist(self, **kwargs):
        self.persisted.append(kwargs)


@pytest.mark.asyncio
async def test_executor_sends_one_batched_detail_call_per_platform() -> None:
    targets = select_top20_detail_targets(
        [
            _item("douyin", "dy-1", 300),
            _item("douyin", "dy-2", 200),
            _item("xiaohongshu", "xhs-1", 100),
        ]
    )
    enrichment = _FakeDetailEnrichment(
        DetailEnrichmentPlan(
            selection_set_id="set-1", groups=group_detail_targets_by_platform(targets)
        )
    )
    gateway = _FakeGateway()
    executor = TaskExecutor(
        repository=None, context_builder=None, planner=None, gateway=gateway,
        detail_enrichment=enrichment, worker_id="worker", lease_seconds=60,
    )
    trajectory = AgentTrajectory()
    task = SimpleNamespace(id="task-1", user_id="user-1", session_id="session-1")

    failed, lease_failed = await executor._enrich_top20_details(
        task=task,
        goal_id="goal-1",
        trajectory=trajectory,
        step_prefix="step",
        persist=_true,
    )

    assert not failed
    assert not lease_failed
    assert [(call.arguments["platform"], call.arguments["kwUidList"]) for call in gateway.commands] == [
        ("douyin", ["dy-1", "dy-2"]),
        ("xiaohongshu", ["xhs-1"]),
    ]
    assert all(
        call.arguments["scope"] == ["fansAudience", "postSummaryStatistics", "accountTrend"]
        for call in gateway.commands
    )
    assert [step.status for step in trajectory.detail_enrichment_steps] == ["succeeded", "succeeded"]
    assert len(enrichment.persisted) == 2


async def _true() -> bool:
    return True
