"""确定性计算工具测试（设计文档 §10.3）。

覆盖五个零积分确定性工具：
calculate_expression / aggregate_metrics / calculate_period_comparison /
normalize_sentiment / rank_kols。rank_kols 复用 selection.scoring_v2 严格
八维 missing_as_zero，并默认跨平台 engagement_total 降序 Top20；结果通过
settled 的 tool call 行建立 lineage。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.tools.calculation import (
    AggregateMetricsTool,
    CalculateExpressionTool,
    CalculatePeriodComparisonTool,
    NormalizeSentimentTool,
    RankKolsTool,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.selection.scoring_v2 import SCORE_VERSION_V2

CTX = ToolContext(
    user_id="u-1",
    session_id="s-1",
    run_id="r-1",
    profile_name="session_analyst_v1",
)

# 与 selection/scoring_v2.WEIGHTS_V2 完全一致（§10.3 权重约束，和 = 100）。
EXPECTED_WEIGHTS = {
    "industry_interest": 10,
    "target_region": 8,
    "target_age": 8,
    "engagement": 20,
    "active_follower": 15,
    "content": 15,
    "followers": 10,
    "engagement_follower_ratio": 14,
}


def _summary(result) -> dict:
    assert result.status == "success", result.safe_summary
    return json.loads(result.safe_summary)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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


def _kol_item(
    *,
    uid: str,
    engagement_total: float | None,
    followers: int = 500_000,
    **overrides: object,
) -> dict:
    item = {
        "platform": "小红书",
        "kol_uid": uid,
        "nickname": f"达人{uid}",
        "followers": followers,
        "engagement_total": engagement_total,
        "growth_rate": 0.3,
        "quoted_price": 800,
        "score_inputs": {
            "audience_interests": {"美食": 80},
            "audience_regions": {"上海": 50},
            "audience_age": {"18-24岁": 40, "25至34": 30},
            "average_interactions": 20_000,
            "effective_follower_rate": 60,
            "active_follower_count": 300_000,
            "content_score": 90,
            "interaction_follower_ratio": 3.0,
        },
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# calculate_expression：受限安全表达式求值
# ---------------------------------------------------------------------------


async def test_calculate_expression_arithmetic() -> None:
    tool = CalculateExpressionTool()
    result = await tool.execute(CTX, type(tool).input_model(expression="1 + 2 * 3"))
    data = _summary(result)
    assert data["result"] == 7
    assert data["expression"] == "1 + 2 * 3"


async def test_calculate_expression_uses_variables() -> None:
    tool = CalculateExpressionTool()
    result = await tool.execute(
        CTX, type(tool).input_model(expression="x * y + 1", variables={"x": 2, "y": 3})
    )
    assert _summary(result)["result"] == 7


async def test_calculate_expression_supports_compare() -> None:
    tool = CalculateExpressionTool()
    result = await tool.execute(
        CTX, type(tool).input_model(expression="x > 100", variables={"x": 120})
    )
    assert _summary(result)["result"] is True


async def test_calculate_expression_rejects_arbitrary_code() -> None:
    tool = CalculateExpressionTool()
    for unsafe in (
        "__import__('os').getcwd()",
        "1 + len([])",
        "(1).__class__",
        "[x for x in (1, 2)]",
        "lambda: 1",
    ):
        result = await tool.execute(CTX, type(tool).input_model(expression=unsafe))
        assert result.status == "failed", unsafe
        assert result.safe_summary


# ---------------------------------------------------------------------------
# aggregate_metrics：确定性聚合
# ---------------------------------------------------------------------------


async def test_aggregate_metrics_by_group() -> None:
    tool = AggregateMetricsTool()
    rows = [
        {"brand": "A", "volume": 100, "spend": 10},
        {"brand": "A", "volume": 200, "spend": 20},
        {"brand": "B", "volume": 50, "spend": 5},
    ]
    metrics = [
        {"name": "total_volume", "field": "volume", "op": "sum"},
        {"name": "avg_spend", "field": "spend", "op": "avg"},
        {"name": "min_volume", "field": "volume", "op": "min"},
        {"name": "max_volume", "field": "volume", "op": "max"},
        {"name": "count_volume", "field": "volume", "op": "count"},
    ]
    result = await tool.execute(
        CTX, type(tool).input_model(rows=rows, group_by="brand", metrics=metrics)
    )
    data = _summary(result)
    groups = {g["group"]["brand"]: g for g in data["groups"]}
    assert groups["A"]["total_volume"] == 300
    assert groups["A"]["avg_spend"] == 15
    assert groups["A"]["min_volume"] == 100
    assert groups["A"]["max_volume"] == 200
    assert groups["A"]["count_volume"] == 2
    assert groups["B"]["total_volume"] == 50
    assert data["rows_processed"] == 3


async def test_aggregate_metrics_without_group() -> None:
    tool = AggregateMetricsTool()
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            rows=[{"v": 1}, {"v": 2}, {"v": 3}],
            metrics=[{"name": "sum", "field": "v", "op": "sum"}],
        ),
    )
    data = _summary(result)
    assert data["groups"] == [{"group": None, "sum": 6}]


async def test_aggregate_metrics_skips_missing_values_and_deterministic_order() -> None:
    tool = AggregateMetricsTool()
    rows = [{"k": "b", "v": 10}, {"k": "a", "v": 20}, {"k": "a"}]
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            rows=rows,
            group_by="k",
            metrics=[{"name": "sum", "field": "v", "op": "sum"}, {"name": "cnt", "field": "v", "op": "count"}],
        ),
    )
    data = _summary(result)
    # 组顺序确定性：按组键排序；缺失值不计入 sum。
    assert [g["group"]["k"] for g in data["groups"]] == ["a", "b"]
    by_key = {g["group"]["k"]: g for g in data["groups"]}
    assert by_key["a"]["sum"] == 20
    assert by_key["a"]["cnt"] == 1
    assert by_key["b"]["sum"] == 10


# ---------------------------------------------------------------------------
# calculate_period_comparison：delta + rate
# ---------------------------------------------------------------------------


async def test_period_comparison_numbers() -> None:
    tool = CalculatePeriodComparisonTool()
    result = await tool.execute(
        CTX, type(tool).input_model(current=120, baseline=100)
    )
    data = _summary(result)
    assert data["delta"] == 20
    assert data["rate"] == pytest.approx(0.2)


async def test_period_comparison_zero_baseline_rate_is_null() -> None:
    tool = CalculatePeriodComparisonTool()
    result = await tool.execute(
        CTX, type(tool).input_model(current=10, baseline=0)
    )
    data = _summary(result)
    assert data["delta"] == 10
    assert data["rate"] is None


async def test_period_comparison_dicts() -> None:
    tool = CalculatePeriodComparisonTool()
    result = await tool.execute(
        CTX,
        type(tool).input_model(current={"声量": 120, "互动": 60}, baseline={"声量": 100, "互动": 80}),
    )
    data = _summary(result)
    assert data["声量"]["delta"] == 20
    assert data["声量"]["rate"] == pytest.approx(0.2)
    assert data["互动"]["delta"] == -20
    assert data["互动"]["rate"] == pytest.approx(-0.25)


# ---------------------------------------------------------------------------
# normalize_sentiment：规范化情感
# ---------------------------------------------------------------------------


async def test_normalize_sentiment_numeric() -> None:
    tool = NormalizeSentimentTool()
    assert _summary(await tool.execute(CTX, type(tool).input_model(raw=0.8)))["sentiment"] == "positive"
    assert _summary(await tool.execute(CTX, type(tool).input_model(raw=-0.5)))["sentiment"] == "negative"
    assert _summary(await tool.execute(CTX, type(tool).input_model(raw=0.1)))["sentiment"] == "neutral"
    assert _summary(await tool.execute(CTX, type(tool).input_model(raw=75)))["sentiment"] == "positive"
    assert _summary(await tool.execute(CTX, type(tool).input_model(raw=20)))["sentiment"] == "negative"


async def test_normalize_sentiment_labels() -> None:
    tool = NormalizeSentimentTool()
    for raw, expected in (
        ("正面", "positive"),
        ("positive", "positive"),
        ("积极", "positive"),
        ("负面", "negative"),
        ("negative", "negative"),
        ("中性", "neutral"),
        ("neutral", "neutral"),
        ("未知标签", "neutral"),
    ):
        data = _summary(await tool.execute(CTX, type(tool).input_model(raw=raw)))
        assert data["sentiment"] == expected, raw
        assert data["polarity"] in (1, 0, -1)


# ---------------------------------------------------------------------------
# rank_kols：严格复用 kol_score_v2
# ---------------------------------------------------------------------------


async def test_rank_kols_uses_strict_scorer_with_exact_weights() -> None:
    tool = RankKolsTool()
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            items=[_kol_item(uid="1", engagement_total=100)],
            context={"industry": "美食", "regions": ["上海", "杭州"], "age_ranges": ["18-24", "25-34"]},
        ),
    )
    data = _summary(result)
    assert data["total"] == 1
    item = data["items"][0]
    snapshot = item["score_snapshot"]
    assert snapshot["version"] == SCORE_VERSION_V2
    assert snapshot["weights"] == EXPECTED_WEIGHTS
    assert set(snapshot["dimensions"].keys()) == set(EXPECTED_WEIGHTS.keys())
    # 每维 raw_score/weight/weighted_score 齐全，weighted_score 由 raw*weight/100 计算。
    for name, dimension in snapshot["dimensions"].items():
        assert dimension["weight"] == EXPECTED_WEIGHTS[name]
        assert dimension["weighted_score"] == pytest.approx(
            round(dimension["raw_score"] * EXPECTED_WEIGHTS[name] / 100, 2)
        )
    assert snapshot["total"] == pytest.approx(
        sum(d["weighted_score"] for d in snapshot["dimensions"].values())
    )


async def test_rank_kols_missing_dimension_is_zero_no_redistribution() -> None:
    tool = RankKolsTool()
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            items=[_kol_item(uid="1", engagement_total=50, score_inputs={"content_score": 80})],
            context={"industry": "美食", "regions": ["上海"], "age_ranges": ["18-24"]},
        ),
    )
    data = _summary(result)
    snapshot = data["items"][0]["score_snapshot"]
    # 仅 content_score=80 → 15 分维度有效：weighted=12，其余维度 0，无权重重分配。
    assert snapshot["dimensions"]["content"]["raw_score"] == 80
    assert snapshot["dimensions"]["engagement"]["raw_score"] == 0
    assert snapshot["dimensions"]["engagement"]["missing_reason"] == "missing_average_interactions"
    assert snapshot["total"] == 12
    assert snapshot["data_completeness"] == 15


async def test_rank_kols_default_top20_engagement_total_desc() -> None:
    tool = RankKolsTool()
    items = [
        _kol_item(uid=f"{i}", engagement_total=float(200 - i * 10)) for i in range(5)
    ]
    # 打乱顺序以验证排序。
    items.reverse()
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            items=items,
            context={"industry": "美食", "regions": ["上海"], "age_ranges": ["18-24"]},
        ),
    )
    data = _summary(result)
    assert [item["kol_uid"] for item in data["items"]] == ["0", "1", "2", "3", "4"]
    assert [item["rank"] for item in data["items"]] == [1, 2, 3, 4, 5]
    assert data["truncated"] is False


async def test_rank_kols_missing_engagement_total_sorted_last_and_limit() -> None:
    tool = RankKolsTool()
    items = [
        _kol_item(uid="a", engagement_total=None),
        _kol_item(uid="b", engagement_total=10),
        _kol_item(uid="c", engagement_total=5),
    ]
    result = await tool.execute(
        CTX,
        type(tool).input_model(
            items=items,
            context={"industry": "美食", "regions": ["上海"], "age_ranges": ["18-24"]},
            limit=2,
        ),
    )
    data = _summary(result)
    # 有 engagement_total 的排前面；limit=2 截断，None 项不进入前 2。
    assert [item["kol_uid"] for item in data["items"]] == ["b", "c"]
    assert data["truncated"] is True


async def test_rank_kols_records_settled_zero_cost_tool_call(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, step = await _make_chain(db_session, user.id)
    tool = RankKolsTool(db_session)
    context = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
        step_id=step.id,
    )
    result = await tool.execute(
        context,
        type(tool).input_model(
            items=[_kol_item(uid="1", engagement_total=100)],
            context={"industry": "美食", "regions": ["上海"], "age_ranges": ["18-24"]},
        ),
    )
    assert result.status == "success"
    assert json.loads(result.safe_summary)["items"][0]["rank"] == 1
    rows = (
        await db_session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "settled"
    assert row.internal_tool_name == "rank_kols"
    assert row.step_id == step.id
    # 零积分确定性工具不产生任何计费。
    assert row.points_reserved == 0
    assert row.points_settled == 0
    assert row.arguments_json is not None
    assert row.arguments_hash
    # logical_call_id 确定性派生：同一 run/step/参数重入复用同一行。
    assert len({row.logical_call_id for row in rows}) == 1
