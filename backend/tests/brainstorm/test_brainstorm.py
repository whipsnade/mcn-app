import json

import pytest

from app.brainstorm.schemas import (
    BrainstormModelOutput,
    BrainstormPeriod,
    BrainstormProfile,
    BrainstormQuestion,
)
from app.model.contracts import ModelAdapterError, StructuredResult
from app.tasks import dependencies
from app.workspace.schemas import SessionCreate
from app.workspace.service import WorkspaceService


class FakeBrainstormModel:
    """按队列返回预设输出；用于替代请求线程内的真实模型适配器。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.requests = []

    async def complete_json(self, request):
        self.requests.append(request)
        output = self._outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return StructuredResult(
            value=output, usage=None, request_id="fake-brainstorm", regeneration_count=0
        )


def _install_model(monkeypatch, model: FakeBrainstormModel) -> None:
    monkeypatch.setattr("app.brainstorm.router.get_model_adapter", lambda: model)


def _full_profile() -> BrainstormProfile:
    return BrainstormProfile(
        brand="欧诗漫",
        category="美妆护肤",
        platforms=["xiaohongshu"],
        audience="18-30 岁女性",
        period=BrainstormPeriod(start="2026-04-01", end="2026-06-30"),
        goal="达人投放",
    )


@pytest.mark.asyncio
async def test_first_round_incomplete_profile_asks_one_question_with_options(
    auth_client_factory, monkeypatch
) -> None:
    client = await auth_client_factory("13900000001")
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="好的，先确认要分析的渠道。",
                question=BrainstormQuestion(
                    text="想在哪些渠道做分析？", options=["小红书", "抖音", "微博"]
                ),
                extracted=BrainstormProfile(brand="欧诗漫"),
            )
        ]
    )
    _install_model(monkeypatch, model)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "我想分析欧诗漫"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["task_id"] is None
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "好的，先确认要分析的渠道。"
    brainstorm_meta = body["message"]["metadata"]["brainstorm"]
    assert brainstorm_meta["ready"] is False
    assert brainstorm_meta["options"] == ["小红书", "抖音", "微博"]
    assert brainstorm_meta["profile_summary"]["brand"] == "欧诗漫"
    assert body["profile"]["brand"] == "欧诗漫"
    assert body["profile"]["category"] is None
    assert body["profile"]["platforms"] == []

    # 模型请求契约：purpose/模板/输入结构（消息历史 + 当前画像 + 关键字表清单）。
    request = model.requests[0]
    assert request.purpose == "brainstorm"
    assert request.template_name == "brainstorm_v1"
    assert request.max_tokens == 2048
    model_input = json.loads(request.messages[-1].content)
    assert model_input["messages"][-1]["content"] == "我想分析欧诗漫"
    assert model_input["current_profile"]["brand"] is None
    assert [item["key"] for item in model_input["parameter_checklist"]] == [
        "brand",
        "category",
        "platforms",
        "audience",
        "period",
        "kol_filters",
        "goal",
        "region",
    ]

    # 画像与问答消息已持久化，metadata 经白名单后仍带 brainstorm 键。
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    messages = restored.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["metadata"]["brainstorm"]["options"] == ["小红书", "抖音", "微博"]
    assert restored.json()["filters"]["brainstorm_profile"]["brand"] == "欧诗漫"
    # ready=false 时不得建任务、不得写回标量列。
    assert restored.json()["latest_task"] is None
    assert restored.json()["brand"] == ""


@pytest.mark.asyncio
async def test_second_round_ready_creates_task_with_trigger_message(
    auth_client_factory, monkeypatch
) -> None:
    client = await auth_client_factory("13900000002")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="好的，先确认要分析的渠道。",
                question=BrainstormQuestion(text="想在哪些渠道分析？", options=["小红书", "抖音"]),
                extracted=BrainstormProfile(brand="欧诗漫"),
            ),
            # 第二轮只回传新确认字段，服务端需与首轮画像合并。
            BrainstormModelOutput(
                ready=True,
                assistant_message="信息已齐，开始分析。",
                extracted=BrainstormProfile(
                    category="美妆护肤",
                    platforms=["xiaohongshu"],
                    audience="18-30 岁女性",
                    period=BrainstormPeriod(start="2026-04-01", end="2026-06-30"),
                    goal="达人投放",
                    region="杭州",
                ),
            ),
        ]
    )
    _install_model(monkeypatch, model)

    first = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "我想分析欧诗漫"}
    )
    assert first.json()["ready"] is False
    second = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "小红书，美妆护肤，看达人投放"},
    )

    assert second.status_code == 200
    body = second.json()
    assert body["ready"] is True
    assert body["task_id"]
    assert body["message"]["metadata"]["brainstorm"]["ready"] is True
    assert body["message"]["metadata"]["brainstorm"]["options"] == []
    # 合并后的完整画像（brand 来自首轮）。
    assert body["profile"] == {
        "brand": "欧诗漫",
        "category": "美妆护肤",
        "platforms": ["xiaohongshu"],
        "audience": "18-30 岁女性",
        "period": {"start": "2026-04-01", "end": "2026-06-30"},
        "kol_filters": None,
        "goal": "达人投放",
        "region": "杭州",
    }

    task = await client.get(f"/api/v1/tasks/{body['task_id']}")
    assert task.status_code == 200
    assert task.json()["status"] == "pending"
    assert task.json()["kind"] == "agent"

    restored = await client.get(f"/api/v1/sessions/{session_id}")
    messages = restored.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]
    # trigger 消息是第二轮的用户消息。
    assert task.json()["trigger_message_id"] == messages[2]["id"]
    # ready 后画像写回 filters_snapshot 与标量列。
    assert restored.json()["filters"]["brainstorm_profile"]["goal"] == "达人投放"
    assert restored.json()["filters"]["brainstorm_profile"]["region"] == "杭州"
    assert restored.json()["brand"] == "欧诗漫"
    assert restored.json()["category"] == "美妆护肤"
    assert restored.json()["platforms"] == ["xiaohongshu"]
    assert restored.json()["target_audience"] == "18-30 岁女性"


@pytest.mark.asyncio
async def test_title_suggestion_updates_default_title_only_while_default(
    auth_client_factory, monkeypatch
) -> None:
    client = await auth_client_factory("13900000003")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    assert created.json()["title"] == "新会话1"
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="确认一下渠道。",
                question=BrainstormQuestion(text="渠道？", options=["小红书"]),
                extracted=BrainstormProfile(brand="欧诗漫"),
                title_suggestion="欧诗漫投放分析",
            ),
            BrainstormModelOutput(
                ready=False,
                assistant_message="再确认一下品类。",
                question=BrainstormQuestion(text="品类？", options=["美妆护肤"]),
                extracted=BrainstormProfile(brand="欧诗漫"),
                title_suggestion="不应再覆盖",
            ),
        ]
    )
    _install_model(monkeypatch, model)

    await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "我想分析欧诗漫"}
    )
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    assert restored.json()["title"] == "欧诗漫投放分析"

    await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "小红书"}
    )
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    assert restored.json()["title"] == "欧诗漫投放分析"


@pytest.mark.asyncio
async def test_blank_title_fallback_increments_and_empty_suggestion_keeps_default(
    auth_client_factory, monkeypatch
) -> None:
    client = await auth_client_factory("13900000004")
    first = await client.post("/api/v1/sessions", json={})
    second = await client.post("/api/v1/sessions", json={})
    assert first.json()["title"] == "新会话1"
    assert second.json()["title"] == "新会话2"
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="想分析什么品牌？",
                question=BrainstormQuestion(text="品牌？", options=["欧诗漫", "珀莱雅"]),
                title_suggestion="",
            )
        ]
    )
    _install_model(monkeypatch, model)

    await client.post(
        f"/api/v1/sessions/{second.json()['id']}/brainstorm", json={"content": "随便看看"}
    )

    restored = await client.get(f"/api/v1/sessions/{second.json()['id']}")
    assert restored.json()["title"] == "新会话2"


@pytest.mark.asyncio
async def test_brainstorm_requires_owner(auth_client_factory, monkeypatch) -> None:
    owner = await auth_client_factory("13900000005")
    outsider = await auth_client_factory("13900000006")
    created = await owner.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="确认一下渠道。",
                question=BrainstormQuestion(text="渠道？", options=["小红书"]),
            )
        ]
    )
    _install_model(monkeypatch, model)

    forbidden = await outsider.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "越权访问"}
    )
    assert forbidden.status_code == 404
    assert model.requests == []

    allowed = await owner.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "我想分析欧诗漫"}
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_brainstorm_without_token_returns_401(client, monkeypatch) -> None:
    model = FakeBrainstormModel([])
    _install_model(monkeypatch, model)
    response = await client.post(
        "/api/v1/sessions/00000000-0000-0000-0000-000000000000/brainstorm",
        json={"content": "未登录"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_brainstorm_model_error_returns_friendly_502(auth_client_factory, monkeypatch) -> None:
    client = await auth_client_factory("13900000007")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    model = FakeBrainstormModel([ModelAdapterError("MODEL_TIMEOUT", retryable=False)])
    _install_model(monkeypatch, model)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "分析一下欧诗漫"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "BRAINSTORM_MODEL_ERROR"


def _patch_runtime(monkeypatch, db_session) -> None:
    """把运行时依赖改到测试会话：SessionFactory 直出测试连接，工具目录置空。"""

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_):
            return None

    class _StubRegistry:
        def __init__(self, *_args):
            pass

        async def list_enabled(self):
            return []

    monkeypatch.setattr(dependencies, "SessionFactory", lambda: _SessionCM())
    monkeypatch.setattr(dependencies, "ToolRegistryService", _StubRegistry)


@pytest.mark.asyncio
async def test_build_agent_context_injects_param_profile_and_overrides_period(
    db_session, user_factory, monkeypatch
) -> None:
    user = await user_factory()
    profile = {
        "brand": "欧诗漫",
        "category": "美妆护肤",
        "platforms": ["xiaohongshu"],
        "audience": None,
        "period": {"start": "2026-04-01", "end": "2026-06-30"},
        "kol_filters": None,
        "goal": "达人投放",
    }
    workspace = await WorkspaceService(db_session).create_session(
        user.id, SessionCreate(filters={"brainstorm_profile": profile})
    )
    _patch_runtime(monkeypatch, db_session)

    context = await dependencies.TaskExecutionDependencies().build_agent_context(
        user.id, workspace.id
    )

    assert context.param_profile == profile
    assert context.requested_period["start"] == "2026-04-01"
    assert context.requested_period["end"] == "2026-06-30"


@pytest.mark.asyncio
async def test_build_agent_context_without_profile_keeps_text_period(
    db_session, user_factory, monkeypatch
) -> None:
    user = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(user.id, SessionCreate())
    _patch_runtime(monkeypatch, db_session)

    context = await dependencies.TaskExecutionDependencies().build_agent_context(
        user.id, workspace.id
    )

    assert context.param_profile == {}
    # 无画像时沿用消息文本解析出的默认时间窗（近 3 个月）。
    assert context.requested_period["unit"] == "month"
    assert context.requested_period["value"] == 3


def test_parameter_checklist_includes_region_and_prompt_mentions_it() -> None:
    from app.brainstorm.parameters import BRAINSTORM_PARAMETERS
    from app.model.prompts import BRAINSTORM_PROMPT

    keys = [item["key"] for item in BRAINSTORM_PARAMETERS]
    assert "region" in keys
    entry = next(item for item in BRAINSTORM_PARAMETERS if item["key"] == "region")
    assert entry["label"] == "目标地区"
    assert "region" in BRAINSTORM_PROMPT.system


def test_param_profile_period_override_validation() -> None:
    override = dependencies.param_profile_period_override
    assert override({}) is None
    assert override({"period": "近3个月"}) is None
    assert override({"period": {"start": "2026-07-01", "end": "2026-06-01"}}) is None
    assert override({"period": {"start": "not-a-date", "end": "2026-06-01"}}) is None
    assert override({"period": {"start": "2026-04-01", "end": "2026-04-30"}}) == {
        "unit": "day",
        "value": 29,
        "start": "2026-04-01",
        "end": "2026-04-30",
    }
