"""kol_selection_v3 Draft builder tests（设计 §12.1 / Task 16）。

覆盖：
1. 严格评分复用：builder 委托 rank_kols → kol_score_v2 八维加权；缺失维度记 0
   且不重分配权重；growth_rate/quoted_price 只展示不进总分；
2. 完整 score_snapshot 冻结：8 个维度每项 {raw_score,weight,weighted_score,
   source,missing_reason}；缺维度的 snapshot 被 Schema 拒绝；
3. 默认 Top20 + engagement_total 降序；数据不足产出 restricted 产物；
4. lineage：维度原始输入引用 Evidence，评分派生引用已 settled 的 rank_kols
   调用，Task 11 校验器接受 builder 输出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft
from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageOwner,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.payloads.kol_selection import (
    SCORE_DIMENSIONS,
    ScoreSnapshot,
    KolSelectionV3,
)
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.selection.scoring_v2 import (
    SCORE_VERSION_V2,
    ScoreContextV2,
    ScoreInputsV2,
    score_candidate_v2,
)

SCOPE = {
    "category": "美食",
    "platforms": ["小红书", "抖音"],
    "audience": {
        "regions": ["上海", "杭州"],
        "age_ranges": ["18-24", "25-34"],
        "interests": ["美食"],
    },
    "filters": {},
}

LIGHT_CTX = ToolContext(
    user_id="u-1",
    session_id="s-1",
    run_id="r-1",
    profile_name="session_analyst_v1",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _kol_item(
    *,
    uid: str,
    engagement_total: float | None,
    followers: int = 500_000,
    score_inputs: dict[str, Any] | None = None,
    growth_rate: float | None = 0.3,
    quoted_price: int | None = 800,
    **overrides: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "platform": "小红书",
        "kol_uid": uid,
        "nickname": f"达人{uid}",
        "avatar_url": "https://example.com/avatar.png",
        "homepage_url": "https://example.com/home",
        "followers": followers,
        "active_followers": 300_000,
        "active_follower_rate": 0.6,
        "growth_rate": growth_rate,
        "engagement_total": engagement_total,
        "avg_engagement": 1.0,
        "likes": 50,
        "comments": 30,
        "shares": 20,
        "quoted_price": quoted_price,
        "audience": {"regions": ["上海"], "age_ranges": ["18-24"], "interests": ["美食"]},
        "reasons": ["互动率高"],
        "score_inputs": score_inputs
        or {
            "audience_interests": {"美食": 80},
            "audience_regions": {"上海": 50},
            "audience_age": {"18-24岁": 40, "25至34": 30},
            "average_interactions": 20_000,
            "effective_follower_rate": 60,
            "active_follower_count": 300_000,
            "content_score": 90,
            "followers": followers,
            "interaction_follower_ratio": 3.0,
        },
    }
    item.update(overrides)
    return item


async def _make_chain(db_session, user_id: str) -> tuple[AgentSession, AgentRun, AgentStep]:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="会话", status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    return session, run, step


# ---------------------------------------------------------------------------
# 1. 严格评分复用：rank_kols → kol_score_v2
# ---------------------------------------------------------------------------


async def test_builder_delegates_to_strict_kol_score_v2() -> None:
    # content 维度缺失 → raw_score=0 且不重分配权重；其余 7 维保留权重。
    score_inputs = {
        "audience_interests": {"美食": 80},
        "audience_regions": {"上海": 50},
        "audience_age": {"18-24岁": 40},
        "average_interactions": 20_000,
        "effective_follower_rate": 60,
        "active_follower_count": 300_000,
        "followers": 500_000,
        "interaction_follower_ratio": 3.0,
    }
    item = _kol_item(
        uid="1",
        engagement_total=100,
        score_inputs=score_inputs,
        growth_rate=999,
        quoted_price=999_999,
    )
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=[item], context=LIGHT_CTX
    )

    payload = build.payload
    KolSelectionV3.model_validate(payload)
    item_out = payload["data"]["items"][0]
    snapshot = item_out["score_snapshot"]

    assert snapshot["version"] == SCORE_VERSION_V2
    content = snapshot["dimensions"]["content"]
    assert content["raw_score"] == 0
    assert content["weight"] == 15
    assert content["weighted_score"] == 0
    assert content["missing_reason"] == "missing_content_score"
    assert content["source"] is None
    # 缺失维度记 0，不重分配权重：其余维度权重不变，权重和仍为 100。
    assert snapshot["dimensions"]["engagement"]["weight"] == 20
    assert snapshot["dimensions"]["followers"]["weight"] == 10
    assert sum(d["weight"] for d in snapshot["dimensions"].values()) == 100
    assert snapshot["total"] == pytest.approx(
        sum(d["weighted_score"] for d in snapshot["dimensions"].values())
    )

    # 与严格 kol_score_v2 逐维度一致（builder 委托 rank_kols 复用同一评分器）。
    expected = score_candidate_v2(
        ScoreContextV2(industry="美食", regions=("上海", "杭州"), age_ranges=("18-24", "25-34")),
        ScoreInputsV2(**score_inputs),
    )
    assert snapshot["total"] == pytest.approx(expected["total"])
    for dim in SCORE_DIMENSIONS:
        assert snapshot["dimensions"][dim]["raw_score"] == pytest.approx(
            expected["dimensions"][dim]["raw_score"]
        )
        assert snapshot["dimensions"][dim]["weighted_score"] == pytest.approx(
            expected["dimensions"][dim]["weighted_score"]
        )

    # growth_rate / quoted_price 是展示字段，绝不进入 v2 总分。
    assert item_out["growth_rate"] == 999
    assert item_out["quoted_price"] == 999_999
    assert snapshot["total"] == pytest.approx(expected["total"])


async def test_builder_uses_scoring_weights_block_exactly() -> None:
    item = _kol_item(uid="1", engagement_total=100)
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=[item], context=LIGHT_CTX
    )
    scoring = build.payload["data"]["scoring"]
    assert scoring["version"] == SCORE_VERSION_V2
    assert scoring["method"] == "weighted_sum"
    assert scoring["missing_value_policy"] == "missing_as_zero"
    assert scoring["weights"] == {
        "industry_interest": 10,
        "target_region": 8,
        "target_age": 8,
        "engagement": 20,
        "active_follower": 15,
        "content": 15,
        "followers": 10,
        "engagement_follower_ratio": 14,
    }


# ---------------------------------------------------------------------------
# 2. 完整 score_snapshot 冻结
# ---------------------------------------------------------------------------


async def test_score_snapshot_freezes_full_dimension_set() -> None:
    items = [
        _kol_item(uid="1", engagement_total=100),
        _kol_item(uid="2", engagement_total=50, platform="抖音"),
    ]
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=items, context=LIGHT_CTX
    )
    payload = build.payload
    for item_out in payload["data"]["items"]:
        snapshot = item_out["score_snapshot"]
        assert snapshot["version"] == SCORE_VERSION_V2
        assert set(snapshot) == {
            "version",
            "total",
            "rating",
            "stars",
            "data_completeness",
            "dimensions",
        }
        assert set(snapshot["dimensions"]) == set(SCORE_DIMENSIONS)
        for dim in SCORE_DIMENSIONS:
            entry = snapshot["dimensions"][dim]
            assert set(entry) == {"raw_score", "weight", "weighted_score", "source", "missing_reason"}
            assert isinstance(entry["raw_score"], float)
            assert isinstance(entry["weight"], int)
            assert isinstance(entry["weighted_score"], float)
            assert entry["source"] is None or isinstance(entry["source"], str)
            assert entry["missing_reason"] is None or isinstance(entry["missing_reason"], str)


def test_score_snapshot_rejects_missing_dimension() -> None:
    partial = {
        dim: {"raw_score": 1.0, "weight": 10, "weighted_score": 0.1, "source": "x", "missing_reason": None}
        for dim in list(SCORE_DIMENSIONS)[:-1]
    }
    with pytest.raises(ValidationError):
        ScoreSnapshot(
            version=SCORE_VERSION_V2,
            total=1.0,
            rating="观察",
            stars="★★",
            data_completeness=10.0,
            dimensions=partial,
        )


# ---------------------------------------------------------------------------
# 3. 默认 Top20 + engagement_total 降序；数据不足 → restricted
# ---------------------------------------------------------------------------


async def test_default_top20_ordered_by_engagement_total_desc() -> None:
    items = [
        _kol_item(uid=str(i), engagement_total=float(200 - i * 5), platform="小红书" if i % 2 else "抖音")
        for i in range(25)
    ]
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=items, context=LIGHT_CTX
    )
    payload = build.payload
    assert payload["data_status"] == "complete"
    assert len(payload["data"]["items"]) == 20
    totals = [item_out["engagement_total"] for item_out in payload["data"]["items"]]
    assert totals == sorted(totals, reverse=True)
    assert [item_out["rank"] for item_out in payload["data"]["items"]] == list(range(1, 21))
    assert payload["data"]["summary"]["candidate_count"] == 25
    assert payload["data"]["summary"]["selected_count"] == 20
    KolSelectionV3.model_validate(payload)


async def test_sparse_data_produces_restricted_artifact() -> None:
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=[], context=LIGHT_CTX
    )
    payload = build.payload
    assert payload["data_status"] == "restricted"
    assert payload["limitations"]
    assert payload["data"]["items"] == []
    assert payload["data"]["summary"]["candidate_count"] is None
    assert payload["data"]["summary"]["selected_count"] is None
    KolSelectionV3.model_validate(payload)


# ---------------------------------------------------------------------------
# 4. Lineage：Evidence + settled rank_kols
# ---------------------------------------------------------------------------


async def test_lineage_references_evidence_and_settled_rank_kols(
    db_session, user_factory, session_factory, run_factory,
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    _, _, step = await _make_chain(db_session, user.id)
    ctx = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
        step_id=step.id,
    )

    items = [_kol_item(uid="1", engagement_total=100), _kol_item(uid="2", engagement_total=50)]
    source_call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id="src-1",
        service="mcp",
        internal_tool_name="kol_xiaohongshu_search",
        arguments_json={},
        arguments_hash="args-hash",
        status="settled",
        points_reserved=10,
        points_settled=10,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(source_call)
    await db_session.flush()
    evidence = await EvidenceWriter(db_session).write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=source_call.id,
        source_type="mcp",
        source_name="kol 搜索",
        scope_json=None,
        period_json=None,
        raw_payload=items,
    )

    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id=evidence.id, items=items, context=ctx, db=db_session
    )
    assert build.rank_kols_call_id is not None

    # rank_kols 调用已落库为 settled 内部零积分调用。
    call = await db_session.get(AgentToolCall, build.rank_kols_call_id)
    assert call is not None
    assert call.status == "settled"
    assert call.service == "internal"
    assert call.points_settled == 0

    loader = DbLineageLoader(db_session)
    frozen = await validate_and_freeze_lineage(
        payload=build.payload,
        refs=build.evidence_refs,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=loader,
    )

    # 维度原始输入引用 Evidence；派生评分引用 settled rank_kols 调用。
    raw_ref = next(
        r for r in frozen.refs if r.artifact_path == "/data/items/0/score_snapshot/dimensions/engagement/raw_score"
    )
    assert raw_ref.sources[0].evidence_id == evidence.id
    assert raw_ref.derivation is not None
    assert raw_ref.derivation.tool_call_id == build.rank_kols_call_id
    assert raw_ref.derivation.method == "kol_score_v2:engagement"

    weighted_ref = next(
        r for r in frozen.refs if r.artifact_path == "/data/items/0/score_snapshot/dimensions/content/weighted_score"
    )
    assert weighted_ref.derivation is not None
    assert weighted_ref.derivation.tool_call_id == build.rank_kols_call_id

    total_ref = next(r for r in frozen.refs if r.artifact_path == "/data/items/0/score_snapshot/total")
    assert total_ref.derivation is not None
    assert total_ref.derivation.tool_call_id == build.rank_kols_call_id

    # 全部必选 numeric 都被覆盖（missing_lineage 不会触发，校验已成功）。
    assert len(frozen.refs) >= 20
