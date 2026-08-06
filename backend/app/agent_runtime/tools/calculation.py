"""确定性计算工具（设计文档 §10.3）。

五个零积分确定性工具：calculate_expression / aggregate_metrics /
calculate_period_comparison / normalize_sentiment / rank_kols。它们只计算和
校验，绝不决定业务步骤。关键数值进入 Artifact 时通过 settled 的 tool call
行（``lineage.derivation.tool_call_id``）建立字段级来源链。

``rank_kols`` 复用 ``selection.scoring_v3`` 的 ``kol_value_score_v3``（效果 70 +
价格效率 30）：缺失维度记 0 分且不重分配权重；默认跨平台按价值分降序取 Top20；
``preference`` 只改排序主键。``growth_rate`` 与 ``quoted_price`` 只展示，不进入
效果总分（报价参与价格效率）。
"""

from __future__ import annotations

import ast
import json
import operator
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentToolCall
from app.agent_runtime.tools.contracts import (
    ToolContext,
    ToolResult,
    arguments_hash,
    logical_call_id_for,
)
from app.mcp_gateway.validation import McpValidationError
from app.selection.scoring_v3 import (
    CandidateInputV3,
    CandidateScoreV3,
    ScoreContextV3,
    score_and_rank_candidates_v3,
)

# rank_kols 默认跨平台 Top20（§9.3：跨平台合计最多 20）。
_RANK_DEFAULT_LIMIT = 20
_RANK_MAX_LIMIT = 50

# 受限表达式求值的 AST 节点数上限（防御深度嵌套/超大表达式）。
_MAX_AST_NODES = 200

# DoS 防护（§10.3）：超大指数先于 pow 求值被拒绝；整数结果位长受限，使
# `**`/`*` 等运算无法构造天文数字。位长上限放宽到 Python int→str 限制
# （4300 位）之上，让「结果无法序列化」走结构化错误而非崩溃。
_MAX_POW_EXPONENT = 1000
_MAX_RESULT_BITS = 40_000

_SAFE_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_SAFE_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_SAFE_CMP_OPS: dict[type[ast.cmpop], Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_SAFE_BOOL_OPS: dict[type[ast.boolop], Any] = {
    ast.And: all,
    ast.Or: any,
}

_POSITIVE_LABELS = frozenset(
    {"正面", "正", "积极", "正向", "好评", "满意", "positive", "pos", "up"}
)
_NEGATIVE_LABELS = frozenset(
    {"负面", "负", "消极", "差评", "不满意", "negative", "neg", "down"}
)
_NEUTRAL_LABELS = frozenset({"中性", "中", "一般", "neutral", "neu"})


class _UnsafeExpression(ValueError):
    """表达式包含受限求值器不支持的结构。"""


def _calc_failed(message: str) -> ToolResult:
    return ToolResult(status="failed", safe_summary=str(message)[:500])


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: Any) -> float | None:
    if _is_number(value):
        return float(value)
    return None


def _whole(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _number_distribution(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        key: number
        for key, item in value.items()
        if isinstance(key, str) and (number := _number(item)) is not None and number >= 0
    }


# --------------------------------------------------------------------------- #
# 受限安全表达式求值（calculate_expression）
# --------------------------------------------------------------------------- #


def _evaluate(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise _UnsafeExpression(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id not in variables or not isinstance(variables[node.id], (int, float, bool)):
            raise _UnsafeExpression(f"undefined or non-numeric variable: {node.id!r}")
        return variables[node.id]
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _SAFE_BIN_OPS:
            raise _UnsafeExpression(f"unsupported operator: {type(node.op).__name__}")
        left = _evaluate(node.left, variables)
        right = _evaluate(node.right, variables)
        if type(node.op) is ast.Pow:
            # DoS 防护：先于 pow 拒绝超大指数（如 9**9**9），避免构造天文数字。
            if not isinstance(right, (int, float)) or abs(right) > _MAX_POW_EXPONENT:
                raise _UnsafeExpression("exponent too large")
        result = _SAFE_BIN_OPS[type(node.op)](left, right)
        # 结果量级防护：bit_length() 为 O(1)，限制每一步中间结果的位长。
        if isinstance(result, int) and not isinstance(result, bool):
            if result.bit_length() > _MAX_RESULT_BITS:
                raise _UnsafeExpression("result magnitude too large")
        return result
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _SAFE_UNARY_OPS:
            raise _UnsafeExpression(f"unsupported operator: {type(node.op).__name__}")
        return _SAFE_UNARY_OPS[type(node.op)](_evaluate(node.operand, variables))
    if isinstance(node, ast.BoolOp):
        if type(node.op) not in _SAFE_BOOL_OPS:
            raise _UnsafeExpression(f"unsupported operator: {type(node.op).__name__}")
        values = [_evaluate(value, variables) for value in node.values]
        return _SAFE_BOOL_OPS[type(node.op)](values)
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, variables)
        comparators = [_evaluate(value, variables) for value in node.comparators]
        result = True
        for index, comparator_op in enumerate(node.ops):
            if type(comparator_op) not in _SAFE_CMP_OPS:
                raise _UnsafeExpression(f"unsupported operator: {type(comparator_op).__name__}")
            if not _SAFE_CMP_OPS[type(comparator_op)](
                left if index == 0 else comparators[index - 1], comparators[index]
            ):
                result = False
        return result
    raise _UnsafeExpression(f"unsupported node: {type(node).__name__}")


def evaluate_expression(expression: str, variables: Mapping[str, Any]) -> Any:
    """在受限节点白名单下求值算术表达式；结果必须是数字或布尔。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise _UnsafeExpression(f"invalid expression: {exc}") from exc
    node_count = 0
    for _ in ast.walk(tree):
        node_count += 1
        if node_count > _MAX_AST_NODES:
            raise _UnsafeExpression("expression too large")
    result = _evaluate(tree.body, dict(variables))
    if not isinstance(result, (int, float, bool)):
        raise _UnsafeExpression("expression must evaluate to a number or boolean")
    # 最终结果量级防护：字面量超大整数（无任何运算）同样被拦截。
    if isinstance(result, int) and not isinstance(result, bool):
        if result.bit_length() > _MAX_RESULT_BITS:
            raise _UnsafeExpression("result magnitude too large")
    return result


# --------------------------------------------------------------------------- #
# 计算工具
# --------------------------------------------------------------------------- #


async def _record_settled_call(
    db: AsyncSession | None,
    context: ToolContext,
    internal_tool_name: str,
    arguments: BaseModel,
) -> None:
    """确定性调用成功落库为 settled 零积分 tool call（供 lineage 引用）。

    只有存在 DB 且 ``ToolContext.step_id`` 可用时才落库（否则跳过）。
    ``logical_call_id`` 由 run+step+工具+参数哈希确定性派生；正常重入经预查
    幂等复用，并发 TOCTOU 由唯一约束 + savepoint + expunge 兜底（镜像
    mcp.py 的模式），绝不崩溃也不重复落库。
    """
    if db is None or context.step_id is None:
        return
    normalized = arguments.model_dump() if isinstance(arguments, BaseModel) else dict(arguments)
    try:
        args_hash = arguments_hash(normalized)
    except McpValidationError:
        return
    logical_call_id = logical_call_id_for(
        context.run_id, internal_tool_name, args_hash)    
    existing = await db.scalar(
        select(AgentToolCall.id).where(AgentToolCall.logical_call_id == logical_call_id)
    )
    if existing is not None:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    row = AgentToolCall(
        id=str(uuid4()),
        run_id=context.run_id,
        step_id=context.step_id,
        logical_call_id=logical_call_id,
        service="internal",
        internal_tool_name=internal_tool_name,
        arguments_json=normalized,
        arguments_hash=args_hash,
        status="settled",
        points_reserved=0,
        points_settled=0,
        started_at=now,
        completed_at=now,
    )
    try:
        # 在既有事务内用 savepoint 隔离插入，避免唯一约束冲突毒化外层事务。
        if db.in_transaction():
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        else:
            async with db.begin():
                db.add(row)
                await db.flush()
    except IntegrityError:
        if row in db:
            db.expunge(row)
        # 并发 TOCTOU：另一调用已插入同一 logical_call_id，幂等复用其行。
        return


class CalculateExpressionArgs(BaseModel):
    expression: str = Field(min_length=1, max_length=500)
    variables: dict[str, float] = {}


class CalculateExpressionTool:
    """受限算术表达式求值（零积分）。"""

    name = "calculate_expression"
    input_model = CalculateExpressionArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = CalculateExpressionArgs.model_validate(arguments)
        try:
            result = evaluate_expression(args.expression, args.variables)
        except _UnsafeExpression as exc:
            return _calc_failed(str(exc))
        except ZeroDivisionError:
            return _calc_failed("division by zero")
        except (TypeError, ValueError, OverflowError, KeyError) as exc:
            return _calc_failed(f"invalid expression: {exc}")
        try:
            summary = json.dumps(
                {"expression": args.expression, "result": result}, ensure_ascii=False
            )
        except (TypeError, ValueError) as exc:
            # Python int→str 有 4300 位上限：结果过大时转为结构化错误而非崩溃。
            return _calc_failed(f"result too large to serialize: {exc}")
        await _record_settled_call(self._db, context, self.name, args)
        return ToolResult(status="success", safe_summary=summary)


class MetricSpec(BaseModel):
    name: str = Field(min_length=1)
    field: str = Field(min_length=1)
    op: Literal["sum", "avg", "count", "min", "max"]


class AggregateMetricsArgs(BaseModel):
    rows: list[dict[str, Any]] = Field(min_length=1)
    group_by: str | list[str] | None = None
    metrics: list[MetricSpec] = Field(min_length=1)


class AggregateMetricsTool:
    """数据切片上的确定性聚合（sum/avg/count/min/max，零积分）。"""

    name = "aggregate_metrics"
    input_model = AggregateMetricsArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = AggregateMetricsArgs.model_validate(arguments)
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in args.rows:
            key = _group_key(row, args.group_by)
            groups.setdefault(key, []).append(row)

        results: list[dict[str, Any]] = []
        for key in _ordered_group_keys(groups):
            group_rows = groups[key]
            metric_output: dict[str, Any] = {}
            for spec in args.metrics:
                metric_output[spec.name] = _aggregate(group_rows, spec)
            results.append({"group": _group_value(key, args.group_by), **metric_output})

        await _record_settled_call(self._db, context, self.name, args)
        return ToolResult(
            status="success",
            safe_summary=json.dumps(
                {"groups": results, "rows_processed": len(args.rows)}, ensure_ascii=False
            ),
        )


class CalculatePeriodComparisonArgs(BaseModel):
    current: Any
    baseline: Any


class CalculatePeriodComparisonTool:
    """周期对比：delta + rate（零积分）。"""

    name = "calculate_period_comparison"
    input_model = CalculatePeriodComparisonArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = CalculatePeriodComparisonArgs.model_validate(arguments)
        try:
            result = _period_comparison(args.current, args.baseline)
        except ValueError as exc:
            return _calc_failed(str(exc))
        await _record_settled_call(self._db, context, self.name, args)
        return ToolResult(status="success", safe_summary=json.dumps(result, ensure_ascii=False))


class NormalizeSentimentArgs(BaseModel):
    raw: Any


class NormalizeSentimentTool:
    """把情感值映射到规范 positive/neutral/negative 形状（零积分）。"""

    name = "normalize_sentiment"
    input_model = NormalizeSentimentArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = NormalizeSentimentArgs.model_validate(arguments)
        try:
            sentiment, polarity = _normalize_sentiment(args.raw)
        except ValueError as exc:
            return _calc_failed(str(exc))
        await _record_settled_call(self._db, context, self.name, args)
        return ToolResult(
            status="success",
            safe_summary=json.dumps(
                {"sentiment": sentiment, "polarity": polarity, "raw": args.raw},
                ensure_ascii=False,
            ),
        )


class RankKolsArgs(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1)
    context: dict[str, Any] | None = None
    limit: int = Field(default=_RANK_DEFAULT_LIMIT, ge=1, le=_RANK_MAX_LIMIT)
    # v3：preference 只改排序主键（balanced=value / effect=effect / price=price）。
    preference: Literal["effect", "balanced", "price"] = "balanced"
    # 用户确认的内容形式：报价必须匹配其一才计为有效报价。
    content_formats: list[str] = Field(default_factory=list)
    order_by: str = "value_score"
    desc: bool = True


class RankKolsTool:
    """KOL 排序：严格复用 kol_value_score_v3（效果 70 + 价格效率 30）。

    score_inputs 是效果维度的唯一来源；顶层 followers / engagement_total /
    growth_rate / quoted_price 只作展示，quoted_price 参与价格效率。
    """

    name = "rank_kols"
    input_model = RankKolsArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = RankKolsArgs.model_validate(arguments)
        score_context = _score_context_v3(args.context, args.content_formats)
        candidates = [_score_inputs_v3(item, score_context) for item in args.items]
        ranked = score_and_rank_candidates_v3(
            score_context, candidates, args.preference
        )
        scored: list[dict[str, Any]] = []
        for result in ranked:
            scored.append(_rank_entry(result))
        page = scored[: args.limit]
        await _record_settled_call(self._db, context, self.name, args)
        return ToolResult(
            status="success",
            safe_summary=json.dumps(
                {"items": page, "total": len(scored), "truncated": len(scored) > args.limit},
                ensure_ascii=False,
            ),
        )


# --------------------------------------------------------------------------- #
# 纯函数辅助
# --------------------------------------------------------------------------- #


def _group_key(row: Mapping[str, Any], group_by: str | list[str] | None) -> tuple[Any, ...]:
    if group_by is None:
        return ()
    keys = [group_by] if isinstance(group_by, str) else list(group_by)
    return tuple(row.get(key) for key in keys)


def _group_value(key: tuple[Any, ...], group_by: str | list[str] | None) -> Any:
    if group_by is None:
        return None
    keys = [group_by] if isinstance(group_by, str) else list(group_by)
    return dict(zip(keys, key, strict=True))


def _ordered_group_keys(groups: Mapping[tuple[Any, ...], Any]) -> list[tuple[Any, ...]]:
    # 确定性顺序：按规范化字符串键排序，避免哈希/插入序不确定。
    return sorted(groups.keys(), key=lambda key: tuple(str(value) for value in key))


def _aggregate(rows: list[Mapping[str, Any]], spec: MetricSpec) -> Any:
    values: list[float] = []
    for row in rows:
        value = row.get(spec.field)
        if _is_number(value):
            values.append(float(value))
    if spec.op == "count":
        return len(values)
    if not values:
        return None
    if spec.op == "sum":
        return sum(values)
    if spec.op == "avg":
        return round(sum(values) / len(values), 6)
    if spec.op == "min":
        return min(values)
    if spec.op == "max":
        return max(values)
    raise ValueError(f"unknown op: {spec.op}")


def _rate(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / baseline


def _period_comparison(current: Any, baseline: Any) -> dict[str, Any]:
    if isinstance(current, dict) and isinstance(baseline, dict):
        keys = sorted(set(current) | set(baseline))
        result: dict[str, Any] = {}
        for key in keys:
            current_value, baseline_value = current.get(key), baseline.get(key)
            if _is_number(current_value) and _is_number(baseline_value):
                result[key] = {
                    "delta": current_value - baseline_value,
                    "rate": _rate(float(current_value), float(baseline_value)),
                }
            else:
                result[key] = {"delta": None, "rate": None}
        return result
    if _is_number(current) and _is_number(baseline):
        return {
            "delta": current - baseline,
            "rate": _rate(float(current), float(baseline)),
        }
    raise ValueError("current and baseline must be both numbers or both dicts of numbers")


def _text_sentiment(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _normalize_sentiment(raw: Any) -> tuple[str, int]:
    if isinstance(raw, bool):
        raise ValueError("sentiment must be numeric or text")
    if _is_number(raw):
        if -1 <= raw <= 1:
            if raw > 0.2:
                return "positive", 1
            if raw < -0.2:
                return "negative", -1
            return "neutral", 0
        if 0 <= raw <= 100:
            if raw > 60:
                return "positive", 1
            if raw < 40:
                return "negative", -1
            return "neutral", 0
        raise ValueError("numeric sentiment out of supported range")
    if isinstance(raw, str):
        key = _text_sentiment(raw)
        if key in _POSITIVE_LABELS:
            return "positive", 1
        if key in _NEGATIVE_LABELS:
            return "negative", -1
        if key in _NEUTRAL_LABELS:
            return "neutral", 0
        # 未知标签保守归为中性。
        return "neutral", 0
    raise ValueError("sentiment must be numeric or text")


def _score_context_v3(
    context: Mapping[str, Any] | None, content_formats: list[str] | None = None
) -> ScoreContextV3:
    if not context:
        return ScoreContextV3(industry="", regions=(), age_ranges=())
    regions = tuple(
        item for item in context.get("regions") or [] if isinstance(item, str) and item.strip()
    )
    ages = tuple(
        item for item in context.get("age_ranges") or [] if isinstance(item, str) and item.strip()
    )
    formats = tuple(
        item for item in content_formats or [] if isinstance(item, str) and item.strip()
    )
    return ScoreContextV3(
        industry=context.get("industry") or "",
        regions=regions,
        age_ranges=ages,
        content_formats=formats,
    )


def _distribution_score_for(
    raw: Mapping[str, Any], key: str, fallback_key: str, targets: tuple[str, ...]
) -> float | None:
    """直接 0–100 分数优先；否则回退到 v2 分布口径（复用 scoring_v2 的
    _distribution_score，不复制公式）。"""
    direct = _number(raw.get(key))
    if direct is not None:
        return direct
    from app.selection.scoring_v2 import _distribution_score

    return _distribution_score(_number_distribution(raw.get(fallback_key)), targets)


def _percentage_0_100(value: float | None) -> float | None:
    """百分比统一 0–100 口径：0.799 → 79.9；79.9 不重复乘 100。"""
    if value is None:
        return None
    if 0 < value <= 1:
        return round(value * 100, 4)
    return value


def _score_inputs_v3(item: Mapping[str, Any], context: ScoreContextV3) -> CandidateInputV3:
    """评分输入只来自 ``score_inputs``（+ 顶层身份/报价）；缺失字段保持 None。

    合法 0 值用显式 None 判断（``a or b`` 会把 0 当缺失）。
    """
    raw = item.get("score_inputs") or {}
    followers = _whole(raw.get("followers"))
    average_interactions = _number(raw.get("average_interactions"))
    ratio = _number(raw.get("interaction_follower_ratio"))
    if ratio is None:
        ratio = _number(raw.get("engagement_follower_ratio"))
    if ratio is None and average_interactions is not None and followers is not None and followers > 0:
        ratio = average_interactions / followers * 100
    active = _percentage_0_100(
        _number(raw.get("active_follower_rate"))
        if raw.get("active_follower_rate") is not None
        else _number(raw.get("effective_follower_rate"))
    )
    if active is None and _whole(raw.get("active_follower_count")) is not None and followers is not None and followers > 0:
        active = _whole(raw.get("active_follower_count")) / followers * 100
    return CandidateInputV3(
        platform=str(item.get("platform") or ""),
        kol_uid=str(item.get("kol_uid") or ""),
        nickname=str(item.get("nickname") or ""),
        average_interactions=average_interactions,
        active_follower_rate=active,
        engagement_follower_ratio=ratio,
        content_match=_number(raw.get("content_score")),
        followers=followers,
        industry_interest=_distribution_score_for(
            raw, "industry_interest", "audience_interests", (context.industry,)
        ),
        target_region=_distribution_score_for(
            raw, "target_region", "audience_regions", context.regions
        ),
        target_age=_distribution_score_for(
            raw, "target_age", "audience_age", context.age_ranges
        ),
        quoted_price=_number(item.get("quoted_price")),
        content_format=str(item.get("content_format") or "") or None,
    )


def _rank_entry(result: CandidateScoreV3) -> dict[str, Any]:
    """把 v3 评分结果映射为 rank_kols 输出条目（含 payload 冻结快照形状）。"""
    return {
        "platform": result.platform,
        "kol_uid": result.kol_uid,
        "nickname": result.nickname,
        "followers": None,
        "engagement_total": None,
        "growth_rate": None,
        "quoted_price": result.quoted_price,
        "rank": result.rank,
        "score_snapshot": {
            "version": "kol_value_score_v3",
            "effect_score": result.effect_score,
            "price_efficiency_score": result.price_efficiency_score,
            "value_score": result.value_score,
            "quoted_price": result.quoted_price,
            "price_sample_size": result.price_sample_size,
            "raw_price_efficiency": result.raw_price_efficiency,
            "price_efficiency_percentile": result.price_efficiency_percentile,
            "rating": result.rating,
            "data_completeness": result.data_completeness,
            "dimensions": {
                name: {
                    "raw_score": dim.raw_score,
                    "weight": dim.weight,
                    "weighted_score": dim.weighted_score,
                    "source": dim.source,
                    "missing_reason": dim.missing_reason,
                }
                for name, dim in result.dimensions.items()
            },
        },
        "missing_fields": [
            name
            for name, dim in result.dimensions.items()
            if dim.missing_reason is not None
        ],
    }


def _sort_entries(entries: list[dict[str, Any]], order_by: str, desc: bool) -> list[dict[str, Any]]:
    def key(entry: dict[str, Any]) -> tuple[int, float]:
        value = entry.get(order_by)
        if not _is_number(value):
            return (1, 0.0)
        return (0, -float(value) if desc else float(value))

    return sorted(entries, key=key)


__all__ = [
    "AggregateMetricsArgs",
    "AggregateMetricsTool",
    "CalculateExpressionArgs",
    "CalculateExpressionTool",
    "CalculatePeriodComparisonArgs",
    "CalculatePeriodComparisonTool",
    "MetricSpec",
    "NormalizeSentimentArgs",
    "NormalizeSentimentTool",
    "RankKolsArgs",
    "RankKolsTool",
    "evaluate_expression",
]
