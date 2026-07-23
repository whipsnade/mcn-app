"""quick 模型小循环：工具按模型决策执行、结果契约校验、护栏行为。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.orchestration.schemas import PlannerTool
from app.quick.agent import (
    QUICK_AGENT_MAX_ROUNDS,
    QuickDecision,
    quick_feature_tool_names,
    run_quick_feature,
)
from app.quick.errors import QuickCallFailedError


_RAW_POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "target_type": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "anys": {"type": ["array", "null"], "items": {"type": "array", "items": {"type": "string"}}},
        "datasource": {"type": "array", "items": {"type": "string"}},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "order_by": {"type": ["string", "null"]},
        "size": {"type": ["integer", "null"]},
    },
    "required": ["target_type", "datasource", "start_time", "end_time"],
    "additionalProperties": False,
}

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {
            "type": "object",
            "properties": {
                "page": {"type": "integer"},
                "size": {"type": "integer"},
                "textContentWord": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        }
    },
    "required": ["request"],
    "additionalProperties": False,
}

TOOLS = (
    PlannerTool(
        catalog_id="c-raw",
        internal_name="datatap.insight.query.raw.posts.v1",
        service=DataTapService.INSIGHT_CUBE,
        description="社媒原帖明细检索",
        input_schema=_RAW_POSTS_SCHEMA,
    ),
    PlannerTool(
        catalog_id="c-search",
        internal_name="datatap.xiaohongshu.kol.search.v1",
        service=DataTapService.SOCIAL_GROW,
        description="小红书 KOL 候选检索",
        input_schema=_SEARCH_SCHEMA,
    ),
)

POST_ROWS = [{"标题": "爆贴", "用户昵称": "作者", "互动数": 100}]


class _ScriptedModel:
    """按脚本返回 QuickDecision；脚本耗尽后强制 finish（空列表）。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[Any] = []

    async def complete_json(self, request: Any) -> StructuredResult[Any]:
        self.requests.append(request)
        payload = self.script.pop(0) if self.script else {"action": "finish", "result": []}
        if callable(payload):
            payload = payload(request)
        return StructuredResult(
            value=QuickDecision.model_validate(payload),
            usage=None,
            request_id=None,
            regeneration_count=0,
        )


class _ScriptedCall:
    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    async def __call__(self, internal_tool_name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((internal_tool_name, dict(arguments)))
        outcome = self.results.get(internal_tool_name, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run_kwargs(db_session: AsyncSession, model: _ScriptedModel, call: _ScriptedCall) -> dict:
    return {
        "db": db_session,
        "model": model,
        "call": call,
        "tools": TOOLS,
        "user_id": "u-1",
        "feature": "top_posts",
        "goal": "获取近30天互动最高的10条帖子",
        "scenario": {"platform": "douyin"},
        "industries": ["美食"],
        "tags": ["quick:top_posts"],
    }


def _call_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "call_tool",
        "internal_tool_name": "datatap.insight.query.raw.posts.v1",
        "arguments": arguments,
    }


_VALID_ARGS = {
    "target_type": "keyword",
    "name": "美食",
    "datasource": ["短视频__抖音"],
    "start_time": "2026-06-20",
    "end_time": "2026-07-20",
    "order_by": "互动数",
    "size": 10,
}


@pytest.mark.asyncio
async def test_model_selected_tool_executes_then_finish(
    db_session: AsyncSession,
) -> None:
    model = _ScriptedModel([_call_payload(_VALID_ARGS), {"action": "finish", "result": POST_ROWS}])
    call = _ScriptedCall({"datatap.insight.query.raw.posts.v1": {"帖子列表": POST_ROWS}})

    result = await run_quick_feature(**_run_kwargs(db_session, model, call))

    assert result == POST_ROWS
    [(tool_name, arguments)] = call.calls
    assert tool_name == "datatap.insight.query.raw.posts.v1"
    assert arguments["datasource"] == ["短视频__抖音"]
    assert arguments["size"] == 10
    # 第二轮的 prompt 中应回填第一轮证据。
    second_user = json.loads(model.requests[1].messages[-1].content)
    assert second_user["evidence"][0]["status"] == "settled"
    # log_context 透传 purpose/tags。
    first_request = model.requests[0]
    assert first_request.purpose == "quick_feature"
    assert first_request.log_context["user_id"] == "u-1"
    assert first_request.log_context["tags"] == ["quick:top_posts"]


@pytest.mark.asyncio
async def test_finish_result_contract_violation_is_fed_back(
    db_session: AsyncSession,
) -> None:
    model = _ScriptedModel(
        [{"action": "finish", "result": 42}, {"action": "finish", "result": POST_ROWS}]
    )
    call = _ScriptedCall()

    result = await run_quick_feature(**_run_kwargs(db_session, model, call))

    assert result == POST_ROWS
    assert call.calls == []
    second_user = json.loads(model.requests[1].messages[-1].content)
    assert second_user["evidence"][0]["tool"] == "output_contract"


@pytest.mark.asyncio
async def test_invalid_decisions_twice_raise(db_session: AsyncSession) -> None:
    model = _ScriptedModel(
        [
            {"action": "call_tool", "internal_tool_name": "datatap.not.allowed.v1", "arguments": {}},
            {"action": "call_tool", "internal_tool_name": "datatap.not.allowed.v1", "arguments": {}},
        ]
    )
    call = _ScriptedCall()

    with pytest.raises(QuickCallFailedError, match="invalid_decision"):
        await run_quick_feature(**_run_kwargs(db_session, model, call))

    assert call.calls == []


@pytest.mark.asyncio
async def test_tool_failure_is_fed_back_and_model_can_finish(
    db_session: AsyncSession,
) -> None:
    model = _ScriptedModel(
        [_call_payload(_VALID_ARGS), {"action": "finish", "result": []}]
    )
    call = _ScriptedCall(
        {"datatap.insight.query.raw.posts.v1": QuickCallFailedError("connection_error")}
    )

    result = await run_quick_feature(**_run_kwargs(db_session, model, call))

    assert result == []
    second_user = json.loads(model.requests[1].messages[-1].content)
    assert second_user["evidence"][0]["status"] == "failed"
    assert "connection_error" in second_user["evidence"][0]["summary"]


@pytest.mark.asyncio
async def test_round_limit_forces_finish(db_session: AsyncSession) -> None:
    def finish_when_forced(request: Any) -> dict[str, Any]:
        user = json.loads(request.messages[-1].content)
        assert user["force_finish"] is True
        return {"action": "finish", "result": POST_ROWS}

    model = _ScriptedModel(
        [_call_payload(_VALID_ARGS)] * QUICK_AGENT_MAX_ROUNDS + [finish_when_forced]
    )
    call = _ScriptedCall({"datatap.insight.query.raw.posts.v1": {"帖子列表": POST_ROWS}})

    result = await run_quick_feature(**_run_kwargs(db_session, model, call))

    assert result == POST_ROWS
    assert len(call.calls) == QUICK_AGENT_MAX_ROUNDS
    assert len(model.requests) == QUICK_AGENT_MAX_ROUNDS + 1


@pytest.mark.asyncio
async def test_forced_round_still_calling_raises(db_session: AsyncSession) -> None:
    model = _ScriptedModel([_call_payload(_VALID_ARGS)] * (QUICK_AGENT_MAX_ROUNDS + 1))
    call = _ScriptedCall({"datatap.insight.query.raw.posts.v1": {}})

    with pytest.raises(QuickCallFailedError, match="round_limit"):
        await run_quick_feature(**_run_kwargs(db_session, model, call))


@pytest.mark.asyncio
async def test_campaign_evaluate_result_contract(db_session: AsyncSession) -> None:
    model = _ScriptedModel(
        [
            # title 超 20 字：不合对象契约，回喂后重 finish。
            {"action": "finish", "result": {"title": "t" * 21, "analysis_markdown": "内容"}},
            {"action": "finish", "result": {"title": "火锅节评估", "analysis_markdown": "# 结论"}},
        ]
    )
    call = _ScriptedCall()

    kwargs = _run_kwargs(db_session, model, call)
    kwargs["feature"] = "campaign_evaluate"
    result = await run_quick_feature(**kwargs)

    assert result.title == "火锅节评估"
    assert result.analysis_markdown == "# 结论"
    assert call.calls == []
    second_user = json.loads(model.requests[1].messages[-1].content)
    assert second_user["evidence"][0]["tool"] == "output_contract"


@pytest.mark.asyncio
async def test_campaign_evaluate_empty_markdown_violates_contract(
    db_session: AsyncSession,
) -> None:
    model = _ScriptedModel(
        [{"action": "finish", "result": {"title": "评估", "analysis_markdown": ""}}] * 2
    )
    call = _ScriptedCall()

    kwargs = _run_kwargs(db_session, model, call)
    kwargs["feature"] = "campaign_evaluate"
    with pytest.raises(QuickCallFailedError, match="invalid_result"):
        await run_quick_feature(**kwargs)


@pytest.mark.asyncio
async def test_system_prompt_override(db_session: AsyncSession) -> None:
    model = _ScriptedModel([{"action": "finish", "result": POST_ROWS}])
    call = _ScriptedCall()

    kwargs = _run_kwargs(db_session, model, call)
    kwargs["system_prompt"] = "自定义系统提示"
    await run_quick_feature(**kwargs)

    assert model.requests[0].messages[0].content == "自定义系统提示"


@pytest.mark.asyncio
async def test_kol_detail_result_contract(db_session: AsyncSession) -> None:
    model = _ScriptedModel(
        [{"action": "finish", "result": {"detail": {"昵称": "达人"}, "posts": POST_ROWS}}]
    )
    call = _ScriptedCall()

    kwargs = _run_kwargs(db_session, model, call)
    kwargs["feature"] = "kol_detail"
    result = await run_quick_feature(**kwargs)

    assert result.detail == {"昵称": "达人"}
    assert result.posts == POST_ROWS
    assert result.posts_degraded is False


@pytest.mark.asyncio
async def test_quick_feature_tool_names_subsets() -> None:
    assert quick_feature_tool_names("top_posts", ("douyin",)) == (
        "datatap.insight.match.best.tag.v1",
        "datatap.insight.query.raw.posts.v1",
    )
    assert set(quick_feature_tool_names("kol_recommend", ("xiaohongshu", "douyin"))) == {
        "datatap.social.grow.kol.match.mentions.tag.v1",
        "datatap.xiaohongshu.kol.search.v1",
        "datatap.douyin.kol.search.v1",
    }
    assert set(quick_feature_tool_names("kol_detail", ("xiaohongshu",))) == {
        "datatap.social.grow.kol.detail.v1",
        "datatap.insight.query.raw.posts.v1",
    }
    assert set(
        quick_feature_tool_names(
            "campaign_evaluate", ("xiaohongshu", "douyin", "bilibili", "weibo", "wechat")
        )
    ) == {
        "datatap.xiaohongshu.kol.search.v1",
        "datatap.douyin.kol.search.v1",
        "datatap.social.grow.kol.bilibili.search.v1",
        "datatap.social.grow.kol.weibo.search.v1",
        "datatap.social.grow.kol.wechat.search.v1",
        "datatap.social.grow.kol.detail.v1",
        "datatap.social.grow.kol.match.mentions.tag.v1",
    }


def test_slim_quick_evidence_keeps_parseable_small_rows() -> None:
    import json

    from app.quick.agent import slim_quick_evidence

    payload = [
        {
            "唯一ID": "id-1",
            "标题": "爆贴",
            "内容": "很长的正文" * 500,
            "互动数": 1000,
            "用户昵称": "作者",
            "发布时间": "2026-07-20",
            "帖子链接": "https://example.com/p/1",
        }
    ]
    slimmed = slim_quick_evidence(payload)

    row = slimmed[0]
    assert "内容" not in row
    assert row["标题"] == "爆贴"
    assert row["互动数"] == 1000
    assert len(json.dumps(slimmed, ensure_ascii=False)) < 500


def test_slim_quick_evidence_passes_through_non_list_payload() -> None:
    from app.quick.agent import slim_quick_evidence

    assert slim_quick_evidence({"result": "文本"}) == {"result": "文本"}
    assert slim_quick_evidence("plain") == "plain"
