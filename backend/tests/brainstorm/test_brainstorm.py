import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.brainstorm.schemas import (
    BrainstormModelOutput,
    BrainstormPeriod,
    BrainstormProfile,
    BrainstormQuestion,
    BrainstormRequest,
    merge_profile,
)
from app.core.config import get_settings
from app.goals.models import TaskGoal
from app.goals.planner import GoalPlannerService
from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalQuestion, GoalSpec
from app.model.contracts import ModelAdapterError, StructuredResult
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter
from app.tasks import dependencies
from app.thinking.service import SessionThinkingService
from app.workspace.models import Message, WorkspaceSession
from app.workspace.schemas import SessionCreate
from app.workspace.service import WorkspaceService


MINIMAX_THINK_RESPONSE = (
    "<think>检查当前画像，确认品牌、品类和平台是否齐全。</think>\n"
    '{"assistant_message":"请确认品类","extracted":{"audience":null,'
    '"brand":"Manner","category":null,"goal":"声量和情感趋势",'
    '"kol_filters":null,"period":null,"platforms":[],"region":null},'
    '"question":{"options":["咖啡/现制饮品"],"text":"请选择品类"},'
    '"ready":false,"title_suggestion":"Manner品牌分析"}'
)


class _MiniMaxCompletions:
    async def create(self, **kwargs):
        assert kwargs["stream"] is True
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=MINIMAX_THINK_RESPONSE,
                            reasoning_content=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
                _request_id="req-minimax",
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
                _request_id="req-minimax",
            ),
        ]

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()


class _CapturePromptWriter:
    def __init__(self) -> None:
        self.entries: list[PromptLogEntry] = []

    async def __call__(self, entry: PromptLogEntry) -> None:
        self.entries.append(entry)


class _FailingThinkingSink:
    async def started(self, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def delta(self, text: str, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        raise RuntimeError("sink down")

    async def failed(self, *, attempt: int, error_code: str) -> None:
        raise RuntimeError("sink down")


class FakeBrainstormModel:
    """按队列返回预设输出；用于替代请求线程内的真实模型适配器。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.requests = []

    async def complete_json(self, request):
        self.requests.append(request)
        output = self._outputs.pop(0)
        if request.thinking_sink is not None:
            await request.thinking_sink.started(attempt=1)
            await request.thinking_sink.delta("正在梳理用户需求", attempt=1)
        if isinstance(output, Exception):
            if request.thinking_sink is not None:
                await request.thinking_sink.failed(attempt=1, error_code=output.code)
            raise output
        if request.thinking_sink is not None:
            await request.thinking_sink.completed(attempt=1, duration_ms=12)
        return StructuredResult(
            value=output, usage=None, request_id="fake-brainstorm", regeneration_count=0
        )


def _install_model(monkeypatch, model: FakeBrainstormModel) -> None:
    monkeypatch.setattr("app.brainstorm.router.get_model_adapter", lambda: model)


def _share_session_factory(monkeypatch, db_session) -> None:
    """commit 后的思考持久化走 SessionFactory 独立事务；测试 fixture 是共享连接 +
    savepoint，真实 SessionFactory 的新连接看不到未提交数据，需替换为共享会话。"""

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return None

    class _SessionFactory:
        @staticmethod
        def begin():
            return _SessionCM()

    monkeypatch.setattr("app.brainstorm.router.SessionFactory", _SessionFactory)


def _full_profile() -> BrainstormProfile:
    return BrainstormProfile(
        brand="欧诗漫",
        category="美妆护肤",
        platforms=["xiaohongshu"],
        audience="18-30 岁女性",
        period=BrainstormPeriod(start="2026-04-01", end="2026-06-30"),
        goal="达人投放",
    )


def test_brainstorm_request_accepts_turn_id_and_rejects_invalid_uuid() -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"

    assert str(BrainstormRequest(content="分析品牌", turn_id=turn_id).turn_id) == turn_id
    with pytest.raises(ValidationError):
        BrainstormRequest(content="分析品牌", turn_id="not-a-uuid")


@pytest.mark.asyncio
async def test_minimax_think_response_returns_json_message_and_persists_thinking(
    auth_client_factory, db_session, monkeypatch
) -> None:
    writer = _CapturePromptWriter()
    model = TencentPlanAdapter(
        client=_MiniMaxCompletions(),
        log_writer=writer,
        stream_support_cache={},
    )
    _install_model(monkeypatch, model)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13900000011")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析 Manner", "turn_id": turn_id},
    )

    assert response.status_code == 200
    assert response.json()["message"]["content"] == "请确认品类"
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    assistant = restored.json()["messages"][-1]
    [block] = assistant["metadata"]["thinking"]["blocks"]
    assert block["content"] == "检查当前画像，确认品牌、品类和平台是否齐全。"
    assert "<think>" not in assistant["content"]
    [entry] = writer.entries
    assert entry.status == "success"
    assert entry.response == MINIMAX_THINK_RESPONSE


@pytest.mark.asyncio
async def test_brainstorm_succeeds_when_every_thinking_sink_method_fails(
    auth_client_factory, monkeypatch
) -> None:
    monkeypatch.setattr(
        SessionThinkingService,
        "create_sink",
        lambda _self, _spec: _FailingThinkingSink(),
    )
    model = TencentPlanAdapter(
        client=_MiniMaxCompletions(),
        log_writer=_CapturePromptWriter(),
        stream_support_cache={},
    )
    _install_model(monkeypatch, model)
    client = await auth_client_factory("13900000012")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析 Manner"},
    )

    assert response.status_code == 200
    assert response.json()["message"]["content"] == "请确认品类"


@pytest.mark.asyncio
async def test_first_round_incomplete_profile_asks_one_question_with_options(
    auth_client_factory, db_session, monkeypatch
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
    _share_session_factory(monkeypatch, db_session)

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
    assert request.thinking_sink is not None
    assert request.template_name == "brainstorm_v1"
    assert request.max_tokens == 6144
    model_input = json.loads(request.messages[-1].content)
    assert model_input["current_date"] == date.today().isoformat()
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
    assert messages[0]["metadata"]["turn_id"] == messages[1]["metadata"]["turn_id"]
    assert messages[1]["metadata"]["thinking"]["blocks"][0]["label"] == "正在理解需求"
    assert messages[1]["metadata"]["brainstorm"]["options"] == ["小红书", "抖音", "微博"]
    assert restored.json()["filters"]["brainstorm_profile"]["brand"] == "欧诗漫"
    # ready=false 时不得建任务、不得写回标量列。
    assert restored.json()["latest_task"] is None
    assert restored.json()["brand"] == ""


@pytest.mark.asyncio
async def test_second_round_ready_creates_task_with_trigger_message(
    auth_client_factory, db_session, monkeypatch
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
    _share_session_factory(monkeypatch, db_session)

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
async def test_brainstorm_ready_task_actually_exists(
    auth_client_factory, db_session, monkeypatch
) -> None:
    client = await auth_client_factory("13900000013")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=True,
                assistant_message="信息已齐，开始分析。",
                extracted=_full_profile(),
            )
        ]
    )
    _install_model(monkeypatch, model)
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "小红书，美妆护肤，看达人投放"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["task_id"]
    # 幽灵任务回归：响应中的 task_id 必须在 analysis_tasks 真实存在
    # （事故根因是请求事务内思考持久化死锁导致 InnoDB 静默回滚，前端无限 404）。
    task = await client.get(f"/api/v1/tasks/{body['task_id']}")
    assert task.status_code == 200
    assert task.json()["kind"] == "agent"


@pytest.mark.asyncio
async def test_brainstorm_ready_survives_thinking_persist_deadlock(
    auth_client_factory, db_session, monkeypatch
) -> None:
    async def _deadlocked_persist(*_args, **_kwargs):
        raise RuntimeError("Deadlock found when trying to get lock")

    monkeypatch.setattr("app.brainstorm.router.persist_turn_thinking", _deadlocked_persist)
    client = await auth_client_factory("13900000014")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=True,
                assistant_message="信息已齐，开始分析。",
                extracted=_full_profile(),
            )
        ]
    )
    _install_model(monkeypatch, model)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "小红书，美妆护肤，看达人投放"},
    )

    # 思考持久化在 commit 后独立事务执行，即使它抛错也不影响响应与已提交数据。
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    task = await client.get(f"/api/v1/tasks/{body['task_id']}")
    assert task.status_code == 200
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    assistant = restored.json()["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "信息已齐，开始分析。"


@pytest.mark.asyncio
async def test_title_suggestion_updates_default_title_only_while_default(
    auth_client_factory, db_session, monkeypatch
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
    _share_session_factory(monkeypatch, db_session)

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
    auth_client_factory, db_session, monkeypatch
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
    _share_session_factory(monkeypatch, db_session)

    await client.post(
        f"/api/v1/sessions/{second.json()['id']}/brainstorm", json={"content": "随便看看"}
    )

    restored = await client.get(f"/api/v1/sessions/{second.json()['id']}")
    assert restored.json()["title"] == "新会话2"


@pytest.mark.asyncio
async def test_brainstorm_requires_owner(auth_client_factory, db_session, monkeypatch) -> None:
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
    _share_session_factory(monkeypatch, db_session)

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
async def test_brainstorm_model_error_returns_friendly_502(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13900000007")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    # 生产中会话来自前一请求且已提交；测试共享事务需显式结束当前 savepoint，
    # 才能验证 brainstorm 本轮 rollback 后的独立失败落库。
    await db_session.commit()
    model = FakeBrainstormModel([ModelAdapterError("MODEL_TIMEOUT", retryable=False)])
    _install_model(monkeypatch, model)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "分析一下欧诗漫"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "BRAINSTORM_MODEL_ERROR"
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    messages = restored.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["metadata"]["turn_id"] == messages[1]["metadata"]["turn_id"]
    assert messages[1]["metadata"]["thinking"]["status"] == "interrupted"
    assert messages[1]["metadata"]["thinking"]["blocks"][0]["label"] == "正在理解需求"


@pytest.mark.asyncio
async def test_brainstorm_model_error_request_transaction_writes_nothing(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """模型失败发生在写阶段之前：请求事务零写入，用户消息仅由失败落库自建一条。"""
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13900000033")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    await db_session.commit()
    model = FakeBrainstormModel([ModelAdapterError("MODEL_TIMEOUT", retryable=False)])
    _install_model(monkeypatch, model)
    turn_id = "5c1f6d1e-2b4a-4b5c-9f6e-7d8c9b0a1f2e"

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析一下欧诗漫", "turn_id": turn_id},
    )

    assert response.status_code == 502
    # 该 turn 的 user 消息只有一条（record_brainstorm_failure 自建）：
    # 请求事务在模型失败前没有任何写入，rollback 后也不会有残留。
    user_message_count = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.session_id == session_id, Message.role == "user")
    )
    assert user_message_count == 1
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    messages = restored.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["metadata"]["turn_id"] == turn_id
    assert messages[1]["metadata"]["thinking"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_brainstorm_ready_write_phase_rereads_profile_and_locks_after_model(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """写阶段以重读的最新画像为 merge base；模型调用全程不持有行锁。"""
    client = await auth_client_factory("13900000034")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]

    events: list[tuple[str, bool]] = []
    real_get_owned_session = WorkspaceService.get_owned_session

    async def spy_get_owned_session(self, user_id, session_id, *, for_update=False):
        events.append(("get_owned_session", for_update))
        return await real_get_owned_session(
            self, user_id, session_id, for_update=for_update
        )

    monkeypatch.setattr(WorkspaceService, "get_owned_session", spy_get_owned_session)

    class _ConcurrentProfileModel(FakeBrainstormModel):
        async def complete_json(self, request):
            events.append(("model", False))
            # 模拟并发请求在模型调用期间把画像品牌推进为「新品牌」：
            # 绕过 identity map 直写行，等价于另一请求已提交的效果。
            await db_session.execute(
                update(WorkspaceSession)
                .where(WorkspaceSession.id == session_id)
                .values(filters_snapshot={"brainstorm_profile": {"brand": "新品牌"}})
                .execution_options(synchronize_session=False)
            )
            return await super().complete_json(request)

    model = _ConcurrentProfileModel(
        [
            BrainstormModelOutput(
                ready=True,
                assistant_message="信息已齐，开始分析。",
                # extracted 不带 brand：merge 保留 base 的品牌，可直接区分 base 新旧。
                extracted=BrainstormProfile(
                    category="美妆护肤",
                    platforms=["xiaohongshu"],
                    audience="18-30 岁女性",
                    goal="达人投放",
                ),
            )
        ]
    )
    _install_model(monkeypatch, model)
    _share_session_factory(monkeypatch, db_session)

    events.clear()
    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "小红书，美妆护肤，看达人投放"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    # 写阶段重读画像：merge base 是并发推进后的「新品牌」而非读阶段的空画像。
    assert body["profile"]["brand"] == "新品牌"
    restored = await client.get(f"/api/v1/sessions/{session_id}")
    assert restored.json()["filters"]["brainstorm_profile"]["brand"] == "新品牌"
    assert restored.json()["brand"] == "新品牌"
    # 锁窗口收窄：模型调用之前不得有任何 FOR UPDATE，模型之后才允许加锁。
    model_index = next(i for i, event in enumerate(events) if event[0] == "model")
    assert all(not locked for _, locked in events[:model_index])
    assert any(locked for _, locked in events[model_index:])


@pytest.mark.asyncio
async def test_brainstorm_model_error_keeps_502_when_failure_record_fails(
    auth_client_factory, db_session, monkeypatch
) -> None:
    async def _failing_record(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    _share_session_factory(monkeypatch, db_session)
    monkeypatch.setattr(
        "app.brainstorm.router.record_brainstorm_failure", _failing_record
    )
    client = await auth_client_factory("13900000008")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    await db_session.commit()
    model = FakeBrainstormModel([ModelAdapterError("MODEL_TIMEOUT", retryable=False)])
    _install_model(monkeypatch, model)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "分析一下欧诗漫"}
    )

    # 思考失败落库再次失败也不得把 502 升级为 500。
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
    # 日期锚点规则：相对时间以 current_date 为基准折算。
    assert "current_date" in BRAINSTORM_PROMPT.system


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


def test_param_profile_period_override_recency_bounds(caplog) -> None:
    """end 在未来或早于 400 天前的窗口拒绝覆写；近期与去年活动复盘窗口放行。"""
    override = dependencies.param_profile_period_override
    today = date.today()

    with caplog.at_level(logging.WARNING, logger="app.tasks.dependencies"):
        # end 在未来 → 拒绝并记 warning。
        assert override(
            {
                "period": {
                    "start": today.isoformat(),
                    "end": (today + timedelta(days=1)).isoformat(),
                }
            }
        ) is None
        # end 早于 400 天前 → 拒绝并记 warning。
        stale_end = today - timedelta(days=401)
        assert override(
            {
                "period": {
                    "start": (stale_end - timedelta(days=30)).isoformat(),
                    "end": stale_end.isoformat(),
                }
            }
        ) is None
    assert caplog.text.count("param_profile_period_override_rejected") == 2

    # 合法近期窗口（近 30 天）→ 正常覆写。
    recent_start = today - timedelta(days=30)
    assert override(
        {"period": {"start": recent_start.isoformat(), "end": today.isoformat()}}
    ) == {
        "unit": "day",
        "value": 30,
        "start": recent_start.isoformat(),
        "end": today.isoformat(),
    }

    # 约 9 个月前的活动复盘窗口（如去年双 11）→ 接受。
    replay_end = today - timedelta(days=270)
    replay_start = replay_end - timedelta(days=10)
    assert override(
        {"period": {"start": replay_start.isoformat(), "end": replay_end.isoformat()}}
    ) == {
        "unit": "day",
        "value": 10,
        "start": replay_start.isoformat(),
        "end": replay_end.isoformat(),
    }


def test_question_multi_defaults_false() -> None:
    q = BrainstormQuestion(text="哪个品牌？", options=["海底捞"])
    assert q.multi is False


def test_merge_profile_platforms_union_preserves_order_and_dedupes() -> None:
    base = BrainstormProfile(brand="问界", platforms=["douyin", "xiaohongshu"])
    incoming = BrainstormProfile(platforms=["xiaohongshu", "bilibili"])
    merged = merge_profile(base, incoming)
    assert merged.platforms == ["douyin", "xiaohongshu", "bilibili"]


def test_merge_profile_platforms_incoming_empty_keeps_base() -> None:
    base = BrainstormProfile(brand="问界", platforms=["douyin"])
    merged = merge_profile(base, BrainstormProfile())
    assert merged.platforms == ["douyin"]


@pytest.mark.asyncio
async def test_brainstorm_question_multi_flag_in_metadata(
    auth_client_factory, db_session, monkeypatch
) -> None:
    client = await auth_client_factory("13900000013")
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    session_id = created.json()["id"]
    model = FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=False,
                assistant_message="好的，再确认要查看的渠道（可多选）。",
                question=BrainstormQuestion(
                    text="在哪些平台查看？",
                    options=["抖音", "小红书", "B站"],
                    multi=True,
                ),
                extracted=BrainstormProfile(brand="欧诗漫"),
            )
        ]
    )
    _install_model(monkeypatch, model)
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm", json={"content": "我想分析欧诗漫"}
    )

    assert response.status_code == 200
    brainstorm_meta = response.json()["message"]["metadata"]["brainstorm"]
    assert brainstorm_meta["multi"] is True


def _goal_spec(
    sequence: int,
    goal_type: str,
    *,
    brand: str = "蓉李记",
    depends_on_sequence: int | None = None,
) -> GoalSpec:
    return GoalSpec(
        sequence=sequence,
        goal_type=goal_type,
        depends_on_sequence=depends_on_sequence,
        params=GoalParams(brand=brand),
        request_evidence="声量与互动数据",
    )


def _ready_brainstorm_model() -> FakeBrainstormModel:
    return FakeBrainstormModel(
        [
            BrainstormModelOutput(
                ready=True,
                assistant_message="信息已齐，开始分析。",
                extracted=_full_profile(),
            )
        ]
    )


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_execute_creates_brand_analysis_goal(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """ready 内联建任务经 GoalPlanner 规划：brand_analysis 不再被强制成 kol_selection。"""
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(
            action="execute", goals=[_goal_spec(1, "brand_analysis")]
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13900000021")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量情感"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["task_id"]
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.task_id == body["task_id"]
    assert goal.goal_type == "brand_analysis"
    assert goal.sequence == 1
    assert goal.params_json["brand"] == "蓉李记"


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_multi_goal_persists_dependency(
    auth_client_factory, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(
            action="execute",
            goals=[
                _goal_spec(1, "brand_analysis"),
                _goal_spec(2, "kol_selection", depends_on_sequence=1),
            ],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13900000022")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量并圈选达人"},
    )

    assert response.status_code == 200
    assert response.json()["task_id"]
    goals = list(
        (await db_session.scalars(select(TaskGoal).order_by(TaskGoal.sequence))).all()
    )
    assert len(goals) == 2
    assert goals[0].goal_type == "brand_analysis"
    assert goals[0].sequence == 1
    assert goals[0].depends_on_goal_id is None
    assert goals[1].goal_type == "kol_selection"
    assert goals[1].sequence == 2
    assert goals[1].depends_on_goal_id == goals[0].id


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_clarify_falls_back_to_kol_selection(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """brainstorm 已判定 ready，planner clarify 不回问，按默认 kol_selection 建任务。"""
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(
            action="clarify",
            question=GoalQuestion(text="想看哪个品牌？", options=["蓉李记"]),
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13900000023")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量情感"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["task_id"]
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_respond_falls_back_to_kol_selection(
    auth_client_factory, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(action="respond", respond_type="context_qa")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13900000024")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量情感"},
    )

    assert response.status_code == 200
    assert response.json()["task_id"]
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_error_falls_back_to_kol_selection(
    auth_client_factory, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)

    async def failing_plan(self, context, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(GoalPlannerService, "plan_context", failing_plan)
    client = await auth_client_factory("13900000025")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量情感"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["task_id"]
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"


@pytest.mark.asyncio
async def test_brainstorm_ready_plan_disabled_skips_planner(
    auth_client_factory, db_session, monkeypatch
) -> None:
    called = False

    async def forbidden_plan(self, context, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("planner must not run when enforce is off")

    monkeypatch.setattr(GoalPlannerService, "plan_context", forbidden_plan)
    client = await auth_client_factory("13900000026")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    _install_model(monkeypatch, _ready_brainstorm_model())
    _share_session_factory(monkeypatch, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/brainstorm",
        json={"content": "分析蓉李记声量情感"},
    )

    assert response.status_code == 200
    assert response.json()["task_id"]
    assert called is False
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"
