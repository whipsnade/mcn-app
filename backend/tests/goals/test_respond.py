import pytest
from pydantic import ValidationError

from app.goals.respond import (
    CONTEXT_QA_FALLBACK_TEXT,
    OUT_OF_SCOPE_TEXT,
    USAGE_GUIDE_TEXT,
    ContextQaAnswer,
    answer_context_qa,
    build_context_qa_evidence,
)
from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalSpec
from app.goals.validation import validate_goal_plan


def test_respond_requires_respond_type_without_goals_or_question() -> None:
    output = GoalPlannerOutput(action="respond", respond_type="context_qa")
    assert output.respond_type == "context_qa"

    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="respond")
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="respond",
            respond_type="usage_help",
            question={"text": "哪个品牌？"},
        )


def test_non_respond_actions_reject_respond_type() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="clarify",
            question={"text": "哪个品牌？"},
            respond_type="out_of_scope",
        )
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="execute",
            respond_type="context_qa",
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="campaign_analysis",
                    params=GoalParams(brand="喜茶", campaign="618"),
                    request_evidence="分析喜茶 618 表现",
                )
            ],
        )


def test_respond_rejects_goals() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="respond",
            respond_type="context_qa",
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="campaign_analysis",
                    params=GoalParams(brand="喜茶", campaign="618"),
                    request_evidence="分析喜茶 618 表现",
                )
            ],
        )


def test_validate_goal_plan_skips_all_checks_for_respond() -> None:
    # 会话已有品牌时，respond 不得触发 brand_source_context_mismatch。
    output = GoalPlannerOutput(action="respond", respond_type="context_qa")
    validate_goal_plan(
        output,
        "为什么上次分析失败了？",
        session_brand="海底捞",
        account_default_brand="喜茶",
    )


def test_unknown_respond_type_rejected() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="respond", respond_type="chat")


@pytest.mark.asyncio
async def test_recent_task_outcomes_projects_latest_three(
    auth_client_factory, db_session
) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.goals.context import recent_task_outcomes
    from app.tasks.models import AnalysisTask
    from app.workspace.models import Message

    client = await auth_client_factory("13400000091")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")
    user_id = me.json()["id"]

    now = datetime(2026, 7, 27, tzinfo=UTC).replace(tzinfo=None)
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        role="user",
        content="触发消息",
        sequence=1,
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()

    for index in range(4):
        db_session.add(
            AnalysisTask(
                id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                trigger_message_id=message.id,
                kind="agent",
                status="failed" if index == 3 else "completed",
                estimated_points=0,
                plan_json=None,
                error_code="no_evidence_collected" if index == 3 else None,
                error_message=(
                    "未采集到有效数据，请调整分析条件后重试。" if index == 3 else None
                ),
                created_at=datetime(2026, 7, 27, index, tzinfo=UTC).replace(tzinfo=None),
                updated_at=datetime(2026, 7, 27, index, tzinfo=UTC).replace(tzinfo=None),
            )
        )
    await db_session.flush()

    outcomes = await recent_task_outcomes(db_session, user_id, session_id)

    assert len(outcomes) == 3
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["error_code"] == "no_evidence_collected"
    assert "error_message" in outcomes[0]


@pytest.mark.asyncio
async def test_build_context_qa_evidence_empty_session(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000092")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")

    evidence = await build_context_qa_evidence(
        db_session, user_id=me.json()["id"], session_id=session_id
    )

    assert evidence["recent_task_outcomes"] == []
    assert evidence["selection"] == []
    assert evidence["reports"] == []


class _FakeQaModel:
    def __init__(self, output):
        self._output = output
        self.requests = []

    async def complete_json(self, request):
        self.requests.append(request)
        if isinstance(self._output, Exception):
            raise self._output
        from app.model.contracts import StructuredResult

        return StructuredResult(
            value=self._output, usage=None, request_id="fake", regeneration_count=0
        )


@pytest.mark.asyncio
async def test_answer_context_qa_returns_model_answer(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000093")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")
    model = _FakeQaModel(ContextQaAnswer(answer="因为圈选时互动率权重最高。"))

    answer = await answer_context_qa(
        db_session,
        model,
        user_id=me.json()["id"],
        session_id=session_id,
        question="为什么圈选这个达人？",
    )

    assert answer == "因为圈选时互动率权重最高。"
    request = model.requests[0]
    assert request.purpose == "context_qa"
    assert request.max_tokens == 4096


@pytest.mark.asyncio
async def test_answer_context_qa_falls_back_on_model_error(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000094")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")
    from app.model.contracts import ModelAdapterError

    model = _FakeQaModel(ModelAdapterError("MODEL_TIMEOUT", retryable=False))

    answer = await answer_context_qa(
        db_session,
        model,
        user_id=me.json()["id"],
        session_id=session_id,
        question="为什么失败？",
    )

    assert answer == CONTEXT_QA_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_answer_context_qa_falls_back_on_blank_answer(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000095")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")
    # 全空白 answer 能通过 min_length=1，strip 后为空的分支真实可达。
    model = _FakeQaModel(ContextQaAnswer(answer="  "))

    answer = await answer_context_qa(
        db_session,
        model,
        user_id=me.json()["id"],
        session_id=session_id,
        question="为什么圈选这个达人？",
    )

    assert answer == CONTEXT_QA_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_selection_projection_orders_by_total_score(
    auth_client_factory, db_session
) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.selection.models import KolSelectionItem, KolSelectionSet

    client = await auth_client_factory("13400000096")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    me = await client.get("/api/v1/users/me")
    user_id = me.json()["id"]

    now = datetime(2026, 7, 27, tzinfo=UTC).replace(tzinfo=None)
    selection_set = KolSelectionSet(
        id=str(uuid4()),
        session_id=session_id,
        version=1,
        status="completed",
        created_at=now,
        updated_at=now,
    )
    db_session.add(selection_set)
    await db_session.flush()

    # 入库顺序（created_at 升序）与总分顺序故意不一致；无 total 的排最后。
    # uid-high 带 6 维评分结构（与 CandidateScore.as_dict 同构），验证 dimensions 投影。
    rows = [
        ("uid-low", 50.0),
        ("uid-high", 90.0),
        ("uid-mid", 70.0),
        ("uid-no-score", None),
    ]
    for index, (kol_uid, total) in enumerate(rows):
        score_json: dict = {} if total is None else {"total": total}
        if kol_uid == "uid-high":
            score_json["dimensions"] = {
                "audience": {"raw_score": 88.0, "weight": 25, "weighted_score": 22.0},
                "content": {"raw_score": 80.0, "weight": 20, "weighted_score": 16.0},
                "engagement": {"raw_score": None, "weight": 20, "weighted_score": 0},
                "budget": {"raw_score": 70.0, "weight": 15, "weighted_score": 10.5},
                "growth": {"raw_score": 60.0, "weight": 10, "weighted_score": 6.0},
                "brand_safety": {"raw_score": 90.0, "weight": 10, "weighted_score": 9.0},
            }
        db_session.add(
            KolSelectionItem(
                id=str(uuid4()),
                user_id=user_id,
                selection_set_id=selection_set.id,
                platform="xiaohongshu",
                kol_uid=kol_uid,
                nickname=kol_uid,
                fields_json={},
                score_json=score_json,
                created_at=datetime(2026, 7, 27, index, tzinfo=UTC).replace(tzinfo=None),
                updated_at=datetime(2026, 7, 27, index, tzinfo=UTC).replace(tzinfo=None),
            )
        )
    await db_session.flush()

    evidence = await build_context_qa_evidence(
        db_session, user_id=user_id, session_id=session_id
    )

    assert [item["nickname"] for item in evidence["selection"]] == [
        "uid-high",
        "uid-mid",
        "uid-low",
        "uid-no-score",
    ]
    high = evidence["selection"][0]
    assert high["dimensions"] == {
        "audience": 88.0,
        "content": 80.0,
        "engagement": None,
        "budget": 70.0,
        "growth": 60.0,
        "brand_safety": 90.0,
    }
    # 无 dimensions 的 item 投影为空对象。
    assert evidence["selection"][1]["dimensions"] == {}


def test_static_texts_are_non_empty_chinese() -> None:
    assert "达人" in USAGE_GUIDE_TEXT
    assert "营销分析" in OUT_OF_SCOPE_TEXT
    assert CONTEXT_QA_FALLBACK_TEXT
