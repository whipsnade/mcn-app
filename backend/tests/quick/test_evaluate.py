"""活动评估：JSON 输入（活动名 + 达人名单）+ 模型小循环查数。"""

import json

import pytest
from sqlalchemy import select

from app.billing.models import Wallet
from app.model.prompts import CAMPAIGN_EVALUATE_PROMPT
from app.quick.models import QuickMcpCall


_XHS_SEARCH = "datatap.xiaohongshu.kol.search.v1"
_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"

SEARCH_RESULT = {
    "KOL 列表": [
        {
            "账号ID (kwUid)": "xhs-1",
            "平台": "xiaohongshu",
            "昵称": "火锅小王",
            "粉丝数": 120000,
        }
    ]
}
DETAIL_RESULT = {
    "详情列表": [
        {
            "账号ID (kwUid)": "xhs-1",
            "昵称": "火锅小王",
            "粉丝数": 120000,
            "平均互动": 3000.0,
        }
    ]
}
FINISH_RESULT = {"title": "夏季火锅节评估", "analysis_markdown": "# 结论\n火锅小王匹配度高"}


def _search_decision(keyword: str) -> dict:
    return {
        "action": "call_tool",
        "internal_tool_name": _XHS_SEARCH,
        "arguments": {"request": {"page": 1, "size": 10, "textContentWord": keyword}},
    }


def _detail_decision() -> dict:
    return {
        "action": "call_tool",
        "internal_tool_name": _DETAIL_TOOL,
        "arguments": {
            "platform": "xiaohongshu",
            "kwUidList": ["xhs-1"],
            "scope": ["fansAudience", "postSummaryStatistics"],
        },
    }


def _decisions() -> list:
    return [
        _search_decision("火锅小王"),
        _detail_decision(),
        {"action": "finish", "result": FINISH_RESULT},
    ]


@pytest.mark.asyncio
async def test_evaluate_json_contract_and_model_loop(quick_client_factory, db_session) -> None:
    client, user, transport, model = await quick_client_factory(
        balance=500, decisions=_decisions()
    )
    transport.results["kol_xiaohongshu_search"] = SEARCH_RESULT
    transport.results["kol_detail"] = DETAIL_RESULT

    response = await client.post(
        "/api/v1/quick/evaluate",
        json={"activity_name": "夏季火锅节", "kol_names": ["火锅小王"]},
    )

    assert response.status_code == 200
    assert response.json() == FINISH_RESULT
    # 小循环 goal 含活动名与全部达人名；system 用活动评估专用 prompt。
    first = model.requests[0]
    assert first.purpose == "quick_feature"
    assert first.messages[0].content == CAMPAIGN_EVALUATE_PROMPT.system
    user_content = json.loads(first.messages[-1].content)
    assert user_content["feature"] == "campaign_evaluate"
    assert "夏季火锅节" in user_content["goal"]
    assert "火锅小王" in user_content["goal"]
    # 工具按模型决策执行并计费留痕（2 次 × 10 积分）。
    [search_args] = transport.called_arguments("kol_xiaohongshu_search")
    assert search_args["request"]["textContentWord"] == "火锅小王"
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 480
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    assert len(rows) == 2
    assert {row.internal_tool_name for row in rows} == {_XHS_SEARCH, _DETAIL_TOOL}


@pytest.mark.asyncio
async def test_evaluate_goal_contains_all_kol_names(quick_client_factory) -> None:
    client, _user, _transport, model = await quick_client_factory(
        decisions=[{"action": "finish", "result": FINISH_RESULT}]
    )

    response = await client.post(
        "/api/v1/quick/evaluate",
        json={"activity_name": "新品发布会", "kol_names": ["达人甲", "达人乙", "达人甲"]},
    )

    assert response.status_code == 200
    user_content = json.loads(model.requests[0].messages[-1].content)
    assert "新品发布会" in user_content["goal"]
    assert "达人甲" in user_content["goal"]
    assert "达人乙" in user_content["goal"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"activity_name": "", "kol_names": ["达人"]},
        {"activity_name": "x" * 101, "kol_names": ["达人"]},
        {"activity_name": "活动", "kol_names": []},
        {"activity_name": "活动", "kol_names": [f"达人{i}" for i in range(21)]},
        {"activity_name": "活动", "kol_names": [""]},
        {"activity_name": "活动", "kol_names": ["x" * 65]},
    ],
)
async def test_evaluate_rejects_invalid_payload(quick_client_factory, payload) -> None:
    client, _user, _transport, _model = await quick_client_factory()

    response = await client.post("/api/v1/quick/evaluate", json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_evaluate_rejects_names_blank_after_strip(quick_client_factory) -> None:
    client, _user, _transport, _model = await quick_client_factory()

    response = await client.post(
        "/api/v1/quick/evaluate",
        json={"activity_name": "活动", "kol_names": ["   "]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_evaluate_insufficient_balance_returns_409(quick_client_factory, db_session) -> None:
    client, user, transport, _model = await quick_client_factory(
        balance=5, decisions=[_search_decision("火锅小王")]
    )
    transport.results["kol_xiaohongshu_search"] = SEARCH_RESULT

    response = await client.post(
        "/api/v1/quick/evaluate",
        json={"activity_name": "夏季火锅节", "kol_names": ["火锅小王"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "INSUFFICIENT_POINTS"
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 5
    assert wallet.reserved == 0


@pytest.mark.asyncio
async def test_evaluate_model_loop_failure_returns_502(quick_client_factory) -> None:
    # finish 结果连续两次不合对象契约（缺 analysis_markdown）→ 小循环报错。
    client, _user, _transport, _model = await quick_client_factory(
        decisions=[{"action": "finish", "result": {"title": "缺字段"}}] * 2
    )

    response = await client.post(
        "/api/v1/quick/evaluate",
        json={"activity_name": "夏季火锅节", "kol_names": ["火锅小王"]},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "QUICK_CALL_FAILED"
