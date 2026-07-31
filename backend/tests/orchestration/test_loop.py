from app.mcp_gateway.contracts import DataTapService
from app.orchestration.loop import (
    AgentDecision,
    AgentLoopContext,
    AgentTrajectory,
    EvidenceNote,
    TrajectoryStep,
    _prune_arguments,
    build_loop_state,
    normalize_agent_arguments,
    restore_agent_trajectory,
    validate_agent_decision,
)
from app.orchestration.schemas import PlanValidationError, PlannerTool

import pytest


def test_prune_arguments_drops_undeclared_nested_keys_in_anyof_variant() -> None:
    """anyOf 包装内层 additionalProperties: False 时，嵌套的编造参数也必须被剔除
    （事故形态：kol.search 的 request 里被模型塞进 explosiveRateMax/Min）。"""
    schema = {
        "type": "object",
        "properties": {
            "request": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "size": {"type": "integer"},
                            "kwProvinceList": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    {"type": "null"},
                ]
            }
        },
    }
    value = {
        "request": {
            "size": 20,
            "kwProvinceList": ["上海市"],
            "explosiveRateMax": 1,
            "explosiveRateMin": 0.01,
        }
    }

    assert _prune_arguments(value, schema) == {
        "request": {"size": 20, "kwProvinceList": ["上海市"]}
    }


_TOOL_NAME = "datatap.insight.social.statistic.overview.v1"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}


def _tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="cat-1",
        internal_name=_TOOL_NAME,
        service=DataTapService.INSIGHT_CUBE,
        description="声量概览",
        input_schema=_TOOL_SCHEMA,
        output_schema={},
    )


def _context() -> AgentLoopContext:
    return AgentLoopContext(
        recent_messages=(),
        tools=(_tool(),),
        allowed_channels=("xiaohongshu", "douyin"),
    )


def _call(arguments: dict | None = None, tool: str = _TOOL_NAME) -> AgentDecision:
    return AgentDecision(
        action="call_tool",
        internal_tool_name=tool,
        arguments={"keyword": "美妆"} if arguments is None else arguments,
        evidence_goal="声量概览",
    )


def test_valid_decision_returns_matched_tool() -> None:
    tool = validate_agent_decision(_call(), _context())

    assert tool.internal_name == _TOOL_NAME


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="TOOL_NOT_ALLOWED"):
        validate_agent_decision(_call(tool="datatap.unknown.tool"), _context())


def test_missing_tool_name_is_rejected() -> None:
    decision = AgentDecision(action="call_tool", arguments={})

    with pytest.raises(PlanValidationError, match="AGENT_DECISION_MISSING_TOOL_NAME"):
        validate_agent_decision(decision, _context())


def test_tool_name_nested_in_arguments_is_rejected() -> None:
    # 模型把 internal_tool_name 误嵌进 arguments（顶层为 None）时，
    # 必须报「缺工具名」而非笼统的工具缺失，以便上层做可恢复修正。
    decision = AgentDecision(
        action="call_tool",
        arguments={"internal_tool_name": _TOOL_NAME, "keyword": "美妆"},
    )

    with pytest.raises(PlanValidationError, match="AGENT_DECISION_MISSING_TOOL_NAME"):
        validate_agent_decision(decision, _context())


def test_undeclared_fields_are_pruned_but_bad_declared_values_are_rejected() -> None:
    # 未声明的多余字段按 pipeline 语义剔除，不影响调用。
    tool = validate_agent_decision(_call(arguments={"keyword": "美妆", "extra": 1}), _context())
    assert tool.internal_name == _TOOL_NAME
    # 已声明字段的非法取值仍然拒绝。
    with pytest.raises(PlanValidationError, match="INVALID_TOOL_ARGUMENTS"):
        validate_agent_decision(_call(arguments={"keyword": 123}), _context())


def test_trajectory_roundtrip_and_restore() -> None:
    trajectory = AgentTrajectory(
        steps=[
            TrajectoryStep(
                id="step_1",
                internal_tool_name=_TOOL_NAME,
                arguments={"keyword": "美妆"},
                evidence_goal="声量",
            )
        ],
        results=[
            EvidenceNote(
                step_id="step_1",
                tool=_TOOL_NAME,
                status="settled",
                summary={"total": 123},
            ),
        ],
    )

    restored = restore_agent_trajectory(trajectory.as_plan_json())

    assert restored.steps[0].arguments == {"keyword": "美妆"}
    assert restored.results[0].status == "settled"


def test_restore_ignores_missing_or_foreign_plan_json() -> None:
    assert restore_agent_trajectory(None).steps == []
    assert restore_agent_trajectory({"steps": []}).steps == []


def _note(tool: str, status: str = "settled", step_id: str = "step_1") -> EvidenceNote:
    return EvidenceNote(step_id=step_id, tool=tool, status=status, summary={"ok": True})


def test_build_loop_state_dedupes_settled_tools_in_order() -> None:
    notes = [
        _note("datatap.insight.match.best.tag.v1", step_id="step_1"),
        _note("datatap.insight.social.statistic.overview.v1", step_id="step_2"),
        # 重复与失败调用不计入 called_tools。
        _note("datatap.insight.match.best.tag.v1", step_id="step_3"),
        _note("datatap.insight.social.statistic.trend.v1", status="failed", step_id="step_4"),
    ]

    called, _gaps = build_loop_state("brand_analysis", notes)

    assert called == (
        "datatap.insight.match.best.tag.v1",
        "datatap.insight.social.statistic.overview.v1",
    )


def test_build_loop_state_brand_gaps_subtract_covered_stages() -> None:
    notes = [
        _note("datatap.insight.match.best.tag.v1", step_id="step_1"),
        _note("datatap.insight.social.statistic.overview.v1", step_id="step_2"),
        _note("datatap.insight.social.statistic.trend.v1", step_id="step_3"),
    ]

    _called, gaps = build_loop_state("brand_analysis", notes)

    # 标签匹配/概览/趋势已覆盖；情感/话题/受众与地域/热帖未映射到任何已调用工具，保留。
    assert gaps == ("情感", "话题", "受众与地域", "热帖")


def test_build_loop_state_unmapped_tools_keep_all_brand_stages() -> None:
    called, gaps = build_loop_state(
        "brand_analysis", [_note("datatap.insight.query.kol.detail.v1")]
    )

    assert called == ("datatap.insight.query.kol.detail.v1",)
    assert gaps == ("标签匹配", "概览", "趋势", "情感", "话题", "受众与地域", "热帖")


def test_build_loop_state_raw_posts_covers_top_posts_stage() -> None:
    _called, gaps = build_loop_state(
        "brand_analysis", [_note("datatap.insight.query.raw.posts.v1")]
    )

    assert "热帖" not in gaps


def test_build_loop_state_non_brand_goal_has_no_stage_gaps() -> None:
    called, gaps = build_loop_state("kol_selection", [_note(_TOOL_NAME)])

    assert called == (_TOOL_NAME,)
    assert gaps == ()


def test_build_loop_state_empty_notes() -> None:
    called, gaps = build_loop_state("brand_analysis", [])

    assert called == ()
    assert gaps == ("标签匹配", "概览", "趋势", "情感", "话题", "受众与地域", "热帖")


def test_trajectory_has_no_call_count_cap() -> None:
    # 调用次数上限已取消：轨迹不再限制 steps/results 长度。
    trajectory = AgentTrajectory(
        steps=[
            TrajectoryStep(
                id=f"step_{index}",
                internal_tool_name=_TOOL_NAME,
                arguments={"keyword": "美妆"},
            )
            for index in range(1, 13)
        ],
        results=[
            EvidenceNote(
                step_id=f"step_{index}",
                tool=_TOOL_NAME,
                status="settled",
                summary={"total": index},
            )
            for index in range(1, 13)
        ],
    )

    restored = restore_agent_trajectory(trajectory.as_plan_json())
    assert len(restored.steps) == 12
    assert len(restored.results) == 12


def test_normalize_maps_platform_aliases_and_dedupes() -> None:
    result = normalize_agent_arguments(
        {"datasource": ["douyin", "bilibili", "B站", "小红书", "短视频__抖音", "weibo"]}
    )

    assert result["datasource"] == ["短视频__抖音", "视频__哔哩哔哩", "小红书", "微博"]


def test_normalize_keeps_unrecognized_datasource_values() -> None:
    result = normalize_agent_arguments({"datasource": ["博客", "电商__淘宝"]})

    assert result["datasource"] == ["博客", "电商__淘宝"]


def test_normalize_clamps_overlong_time_range() -> None:
    result = normalize_agent_arguments(
        {"start_time": "2025-07-01 00:00:00", "end_time": "2026-07-18 23:59:59"}
    )

    assert result["start_time"] == "2025-07-18 23:59:59"
    assert result["end_time"] == "2026-07-18 23:59:59"


def test_normalize_keeps_short_or_unparseable_time_range() -> None:
    short = normalize_agent_arguments({"start_time": "2026-06-18", "end_time": "2026-07-18"})
    assert short["start_time"] == "2026-06-18"
    untouched = normalize_agent_arguments({"start_time": "近期", "end_time": "2026-07-18"})
    assert untouched["start_time"] == "近期"


def test_normalize_fills_keyword_name_from_anys() -> None:
    result = normalize_agent_arguments(
        {"target_type": "keyword", "anys": [["格力", "GREE"]], "datasource": ["小红书"]}
    )

    assert result["name"] == "格力"
    preserved = normalize_agent_arguments({"target_type": "keyword", "name": "小米"})
    assert preserved["name"] == "小米"


def test_normalize_fills_missing_time_range_from_default_period() -> None:
    tool = PlannerTool(
        catalog_id="cat-9",
        internal_name="datatap.insight.social.statistic.overview.v1",
        service=DataTapService.INSIGHT_CUBE,
        description="声量概览",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema={},
    )
    period = {"unit": "month", "value": 3, "start": "2026-04-18", "end": "2026-07-18"}

    filled = normalize_agent_arguments({"name": "小米"}, tool=tool, default_period=period)

    assert filled["start_time"] == "2026-04-18"
    assert filled["end_time"] == "2026-07-18"
    kept = normalize_agent_arguments(
        {"name": "小米", "start_time": "2026-06-01"}, tool=tool, default_period=period
    )
    assert kept["start_time"] == "2026-06-01"
