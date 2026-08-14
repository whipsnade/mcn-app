"""Task 19 API 测试：/api/v1/agent 会话、Run、SSE 与 kol-details（设计 §十五）。

用假执行器替换进程级 executor，验证路由接线；不真正跑引擎。覆盖：
1. Session CRUD 与用户隔离（所有归属失败 → 404）；
2. messages：单事务写消息 + 建 Run（profile=session_analyst_v1），提交后 submit 到执行器；
   Idempotency-Key 幂等（同 key 同 payload → 同 run；不同 payload → 409）；
3. 活动并发：同一 Session 只允许一个活动 session_analyst_v1 Run；
4. Run 生命周期：detail / cancel / resume（仅 paused 恢复出新 Attempt）/ SSE + Last-Event-ID；
5. 内部 Review/Utility Run 不出现在用户可见 Run 列表；
6. kol-details：固定 body、幂等返回现有 Run、与 session_analyst 并发共存、不可指定 Profile。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.kol_detail import KolDetailRunService
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
    MemoryEntry,
)
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.core.security import create_access_token
from app.db.session import SessionFactory, get_db
from app.identity.models import LoginSession, User
from app.main import create_app


class FakeExecutor:
    """记录 submit 的假执行器；不真正执行 Run（保证 Run 停留在 queued）。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, run_id: str) -> None:
        self.submitted.append(run_id)


@pytest_asyncio.fixture
async def agent_client_factory(db_session):
    """建带鉴权头 + 可选 executor / kol_detail 服务覆写的测试客户端。

    返回 ``(client, app)``：app 暴露给需要读取 app.state（如 SSE broker）的用例。
    """
    from app.agent_runtime.router import get_agent_executor, get_kol_detail_service

    clients: list[tuple[AsyncClient, object]] = []

    async def _make(phone: str, *, executor=None, kol_detail_service=None):
        app = create_app()

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        if executor is not None:
            app.dependency_overrides[get_agent_executor] = lambda: executor
        if kol_detail_service is not None:
            app.dependency_overrides[get_kol_detail_service] = lambda: kol_detail_service
        tc = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        login = await tc.post(
            "/api/v1/auth/mock/sms/login",
            json={"phone": phone, "code": "000000"},
        )
        assert login.status_code == 200, login.text
        tc.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        clients.append((tc, app))
        return tc, app

    yield _make
    for tc, _app in clients:
        await tc.aclose()


async def _me_id(client: AsyncClient) -> str:
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _create_session(client: AsyncClient) -> str:
    resp = await client.post("/api/v1/agent/sessions", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_message(
    client: AsyncClient,
    session_id: str,
    content: str,
    *,
    idempotency_key: str | None = None,
):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": content},
        headers=headers,
    )


async def _add_run(db_session, user_id: str, session_id: str, **overrides) -> AgentRun:
    status_value = overrides.pop("status", "queued")
    profile_name = overrides.pop("profile_name", "session_analyst_v1")
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="user",
        visibility="user",
        profile_name=profile_name,
        profile_version="v1",
        model="test-model",
        status=status_value,
        decision_count=0,
        review_count=0,
        revision_count=0,
        **overrides,
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def _add_paused_run(db_session, user_id: str, session_id: str) -> AgentRun:
    now = utc_now()
    run = await _add_run(db_session, user_id, session_id, status="paused", paused_at=now)
    db_session.add(
        AgentRunAttempt(
            id=str(uuid4()),
            run_id=run.id,
            attempt=1,
            started_at=now,
            ended_at=now,
            decision_count=0,
            outcome="paused",
        )
    )
    await db_session.flush()
    return run


async def _setup_active_kol_detail(
    db_session, user_id: str, session_id: str, platform: str, kol_uid: str
) -> str:
    """创建一条活动 kol_detail_v1 Run（queued）+ 其 working head，供幂等命中。"""
    now = utc_now()
    run_id = str(uuid4())
    db_session.add(
        AgentRun(
            id=run_id,
            session_id=session_id,
            user_id=user_id,
            run_kind="user",
            visibility="user",
            profile_name="kol_detail_v1",
            profile_version="v1",
            model="test-model",
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )
    )
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        module="kol",
        artifact_type="kol_detail_v2",
        parent_artifact_id=None,
        artifact_key=f"kol-detail:{platform}:{kol_uid}",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    # Run / Artifact 先 flush，保证 ArtifactDraft.owner_run_id 的 FK 目标存在。
    await db_session.flush()
    db_session.add(
        ArtifactDraft(
            id=str(uuid4()),
            artifact_id=artifact.id,
            session_id=session_id,
            owner_run_id=run_id,
            current_revision=0,
            status="drafting",
            review_count=0,
            revision_count=0,
            updated_at=now,
        )
    )
    await db_session.flush()
    return run_id


# ---------------------------------------------------------------------------
# Session CRUD + 用户隔离
# ---------------------------------------------------------------------------


async def test_session_crud_and_user_isolation(agent_client_factory) -> None:
    alice, _ = await agent_client_factory("13600000001")
    bob, _ = await agent_client_factory("13600000002")

    # create：默认标题「新会话N」递增
    first = await alice.post("/api/v1/agent/sessions", json={})
    assert first.status_code == 201
    first_id = first.json()["id"]
    assert first.json()["title"] == "新会话1"

    second = await alice.post("/api/v1/agent/sessions", json={})
    assert second.status_code == 201
    assert second.json()["title"] == "新会话2"

    # list（仅本人、未归档）
    listed = await alice.get("/api/v1/agent/sessions")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {first_id, second.json()["id"]}
    assert (await bob.get("/api/v1/agent/sessions")).json() == []

    # detail
    detail = await alice.get(f"/api/v1/agent/sessions/{first_id}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "新会话1"

    # patch rename
    patched = await alice.patch(
        f"/api/v1/agent/sessions/{first_id}", json={"title": "品牌分析"}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "品牌分析"

    # soft delete → 204，之后 detail 404
    deleted = await alice.delete(f"/api/v1/agent/sessions/{first_id}")
    assert deleted.status_code == 204
    assert (await alice.get(f"/api/v1/agent/sessions/{first_id}")).status_code == 404

    # 用户隔离：Bob 对 Alice 的会话一律 404（无存在泄露）
    assert (await bob.get(f"/api/v1/agent/sessions/{second.json()['id']}")).status_code == 404
    assert (
        await bob.patch(
            f"/api/v1/agent/sessions/{second.json()['id']}", json={"title": "x"}
        )
    ).status_code == 404
    assert (
        await bob.delete(f"/api/v1/agent/sessions/{second.json()['id']}")
    ).status_code == 404
    assert (
        await bob.post(
            f"/api/v1/agent/sessions/{second.json()['id']}/messages",
            json={"content": "hi"},
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# messages：单事务建 Run + submit + 幂等 + 并发
# ---------------------------------------------------------------------------


async def test_message_creates_run_and_submits_to_executor(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000011", executor=executor)
    session_id = await _create_session(alice)

    resp = await _post_message(alice, session_id, "帮我分析品牌")
    assert resp.status_code == 201
    body = resp.json()
    run_id = body["run_id"]

    # 提交给了执行器；response 携带 run_id
    assert executor.submitted == [run_id]
    assert body["session_id"] == session_id
    assert body["message_id"]
    assert body["status"] == "queued"

    # Run 以 profile=session_analyst_v1 / queued 落库，归属当前用户
    run = await db_session.get(AgentRun, run_id)
    assert run is not None
    assert run.profile_name == "session_analyst_v1"
    assert run.visibility == "user"
    assert run.status == "queued"
    assert run.input_message_id is not None

    # 用户消息已写入且绑定 Run
    message = await db_session.get(AgentMessage, run.input_message_id)
    assert message is not None
    assert message.role == "user"
    assert message.content == "帮我分析品牌"
    assert message.run_id == run_id

    # 首次执行将是 Attempt 1（执行器 claim 路径）
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run_id)
    assert attempt.attempt == 1

    # 归属隔离：他人读取该 Run → 404
    bob, _ = await agent_client_factory("13600000012")
    assert (await bob.get(f"/api/v1/agent/runs/{run_id}")).status_code == 404


async def test_idempotency_key_reuse_and_payload_conflict(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000021", executor=executor)
    session_id = await _create_session(alice)

    first = await _post_message(alice, session_id, "分析六月声量", idempotency_key="k1")
    assert first.status_code == 201
    run_id = first.json()["run_id"]

    # 同 key + 同 payload → 复用同一 run，不新建
    again = await _post_message(alice, session_id, "分析六月声量", idempotency_key="k1")
    assert again.status_code == 201
    assert again.json()["run_id"] == run_id

    # 同 key + 不同 payload → 409
    conflict = await _post_message(alice, session_id, "换个问题", idempotency_key="k1")
    assert conflict.status_code == 409

    # 只创建了一条 user 消息 + 一个 Run
    runs = (
        await db_session.scalars(
            select(AgentRun).where(
                AgentRun.session_id == session_id, AgentRun.profile_name == "session_analyst_v1"
            )
        )
    ).all()
    assert len(runs) == 1


async def test_idempotency_payload_includes_parent_and_version_refs(
    agent_client_factory, db_session
) -> None:
    """幂等 payload 哈希含 parent_run_id 与 artifact_version_ids：同文本但引用
    不同的请求复用同 key 必须 409，不得复用错误 Run（Gate A 审查修复）。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000022", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    run = await _add_run(db_session, user_id, session_id, status="completed")
    version = await _add_artifact_version(db_session, user_id, session_id, run.id)

    headers = {"Idempotency-Key": "k-refs"}
    first = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "基于这份报告分析", "artifact_version_ids": [version.id]},
        headers=headers,
    )
    assert first.status_code == 201

    # 同文本 + 同 key + 不同引用 → 409（payload 不同）。
    conflict = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "基于这份报告分析", "artifact_version_ids": []},
        headers=headers,
    )
    assert conflict.status_code == 409


async def test_idempotency_hash_normalizes_version_refs(
    agent_client_factory, db_session
) -> None:
    """幂等哈希对 Version 引用去重 + 排序归一再哈希：乱序、重复 ID 是同一逻辑
    payload，复用同 key 应幂等返回同一 Run；parent 变化则视为不同 payload。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000023", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    run = await _add_run(db_session, user_id, session_id, status="completed")
    v1 = await _add_artifact_version(db_session, user_id, session_id, run.id)
    v2 = await _add_artifact_version(db_session, user_id, session_id, run.id)

    # 原始请求：乱序 + 重复 ID。
    headers = {"Idempotency-Key": "k-normalize"}
    first = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "复用报告", "artifact_version_ids": [v2.id, v1.id, v2.id]},
        headers=headers,
    )
    assert first.status_code == 201
    run_id = first.json()["run_id"]

    # 乱序/重复归一到同一哈希 → 复用同一 Run。
    replay = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "复用报告", "artifact_version_ids": [v1.id, v1.id, v2.id]},
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.json()["run_id"] == run_id

    # parent 变化 → 不同 payload → 409（同 key 不得复用错误 Run）。
    parent_run = await _add_run(db_session, user_id, session_id, status="completed")
    parent_change = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={
            "content": "复用报告",
            "parent_run_id": parent_run.id,
            "artifact_version_ids": [v1.id, v2.id],
        },
        headers=headers,
    )
    assert parent_change.status_code == 409


async def test_second_active_run_is_rejected(agent_client_factory, db_session) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000031", executor=executor)
    session_id = await _create_session(alice)

    first = await _post_message(alice, session_id, "第一轮分析")
    assert first.status_code == 201
    # 假执行器不消费，第一轮 Run 停在 queued → 仍活动

    second = await _post_message(alice, session_id, "第二轮分析")
    assert second.status_code == 409

    # 历史 Run 存在，但只有一个活动 Run
    runs = (
        await db_session.scalars(
            select(AgentRun).where(AgentRun.session_id == session_id)
        )
    ).all()
    assert len(runs) == 1
    assert runs[0].status == "queued"


async def test_concurrent_messages_serialize_per_session():
    """两个并发 messages POST 只创建一个 Run，另一个 409（Session 行 FOR UPDATE 串行化）。

    用真实 DB 连接（独立事务）跑并发：两个请求必须各自可见对方的提交，否则会
    双双通过 active 检查并撞 uq_agent_messages_session_sequence（500）。
    """
    app = create_app()  # 不覆写 get_db：每个请求用真实 SessionFactory 独立连接
    session_id: str | None = None
    user_id: str | None = None
    try:
        token, session_id, user_id = await _committed_user_with_session()
        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            results = await asyncio.gather(
                client.post(
                    f"/api/v1/agent/sessions/{session_id}/messages",
                    json={"content": "并发一"},
                    headers=headers,
                ),
                client.post(
                    f"/api/v1/agent/sessions/{session_id}/messages",
                    json={"content": "并发二"},
                    headers=headers,
                ),
                return_exceptions=True,
            )

        statuses = sorted(
            resp.status_code for resp in results if not isinstance(resp, Exception)
        )
        # 一个 201 创建 Run，另一个 409（活动并发）——不允许 500
        assert statuses == [201, 409]

        # 只创建了一个 Run + 一条用户消息（无 500、无重复 sequence）
        async with SessionFactory() as db:
            runs = (
                await db.scalars(
                    select(AgentRun).where(AgentRun.session_id == session_id)
                )
            ).all()
            messages = (
                await db.scalars(
                    select(AgentMessage).where(AgentMessage.session_id == session_id)
                )
            ).all()
        assert len(runs) == 1
        assert len(messages) == 1
    finally:
        if session_id is not None and user_id is not None:
            await _purge_committed_user(session_id, user_id)


async def _committed_user_with_session() -> tuple[str, str, str]:
    """真实连接提交 user + login_session + agent_session，返回 (token, session_id, user_id)。"""
    async with SessionFactory() as db:
        now = utc_now()
        user = User(
            id=str(uuid4()),
            nickname="并发用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        await db.flush()
        login = LoginSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=f"race-{uuid4().hex}",
            expires_at=now + timedelta(days=1),
            created_at=now,
            last_seen_at=now,
        )
        db.add(login)
        session = AgentSession(
            id=str(uuid4()),
            user_id=user.id,
            title="并发会话",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(session)
        await db.flush()
        await db.commit()
        token = create_access_token(user_id=user.id, session_id=login.id, role=user.role)
        return token, session.id, user.id


async def _purge_committed_user(session_id: str, user_id: str) -> None:
    """清理真实连接提交的测试行（message↔run 存在循环 FK，先解绑 run_id）。"""
    async with SessionFactory() as db:
        await db.execute(
            update(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .values(run_id=None)
        )
        await db.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
        await db.execute(delete(AgentMessage).where(AgentMessage.session_id == session_id))
        await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# ---------------------------------------------------------------------------
# Run 生命周期：detail / cancel / resume / SSE
# ---------------------------------------------------------------------------


async def test_run_detail_and_cancel(agent_client_factory) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000041", executor=executor)
    session_id = await _create_session(alice)
    run_id = (await _post_message(alice, session_id, "分析")).json()["run_id"]

    detail = await alice.get(f"/api/v1/agent/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["profile_name"] == "session_analyst_v1"
    assert detail.json()["status"] == "queued"
    assert detail.json()["session_id"] == session_id

    cancelled = await alice.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # 已终态再取消幂等返回
    again = await alice.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"

    bob, _ = await agent_client_factory("13600000042")
    assert (await bob.post(f"/api/v1/agent/runs/{run_id}/cancel")).status_code == 404


async def test_resume_only_when_paused_creates_new_attempt(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, app = await agent_client_factory("13600000051", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    paused = await _add_paused_run(db_session, user_id, session_id)

    # 非 paused（queued）的 Run 不能 resume → 409
    queued = await _add_run(db_session, user_id, session_id)
    refused = await alice.post(f"/api/v1/agent/runs/{queued.id}/resume")
    assert refused.status_code == 409

    # 活动 Run 存在时 resume 被互斥拒绝（§5.5 单活动主 Run）；先取消 queued Run
    blocked = await alice.post(f"/api/v1/agent/runs/{paused.id}/resume")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "active_run_in_progress"
    cancelled = await alice.post(f"/api/v1/agent/runs/{queued.id}/cancel")
    assert cancelled.status_code == 200

    # paused 恢复 → 200，新 Attempt 递增
    resumed = await alice.post(f"/api/v1/agent/runs/{paused.id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "running"
    assert executor.submitted == [paused.id]

    attempts = list(
        (
            await db_session.scalars(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == paused.id)
                .order_by(AgentRunAttempt.attempt)
            )
        ).all()
    )
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert attempts[-1].outcome == "running"
    assert attempts[-1].ended_at is None


async def test_resume_cancelled_run_maps_distinct_code(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory("13600000052")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    # 已请求取消（cancel_requested）的 paused Run：不能恢复，且错误码要区分取消语义
    cancelled = await _add_run(
        db_session,
        user_id,
        session_id,
        status="paused",
        cancel_requested=True,
        paused_at=utc_now(),
    )

    resp = await alice.post(f"/api/v1/agent/runs/{cancelled.id}/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "run_cancel_requested"


# ---------------------------------------------------------------------------
# 取消语义（v3 加固 §5.5）：queued/paused/clarification 立即取消；
# running/reviewing 只写 cancel_requested，由 Engine 在安全点收口
# ---------------------------------------------------------------------------


async def _cancelled_events(db_session, run_id: str) -> list[AgentEvent]:
    return [
        event
        for event in (
            await db_session.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
        ).all()
        if event.event_type == "run.cancelled"
    ]


async def test_cancel_queued_run_cancels_immediately_with_terminal_event(
    agent_client_factory, db_session
) -> None:
    """queued Run：API 立即迁移 cancelled 并写恰好一个 run.cancelled 终态事件。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000091", executor=executor)
    session_id = await _create_session(alice)
    run_id = (await _post_message(alice, session_id, "分析")).json()["run_id"]

    resp = await alice.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert len(await _cancelled_events(db_session, run_id)) == 1

    # 已终态再取消幂等：不重复写事件
    again = await alice.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert again.status_code == 200
    assert len(await _cancelled_events(db_session, run_id)) == 1


async def test_cancel_paused_run_cancels_immediately(
    agent_client_factory, db_session
) -> None:
    """paused Run：无在飞执行，API 立即取消（含终态事件）。"""
    alice, _ = await agent_client_factory("13600000092")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    paused = await _add_paused_run(db_session, user_id, session_id)

    resp = await alice.post(f"/api/v1/agent/runs/{paused.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert len(await _cancelled_events(db_session, paused.id)) == 1


async def test_cancel_running_run_marks_cancel_requested_only(
    agent_client_factory, db_session
) -> None:
    """running Run：API 只写 cancel_requested——不写终态、不发 run.cancelled，
    由 Engine 在下一个安全点收口。
    """
    alice, _ = await agent_client_factory("13600000093")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    now = utc_now()
    running = await _add_run(
        db_session,
        user_id,
        session_id,
        status="running",
        started_at=now,
        lease_owner="worker-a",
        lease_expires_at=now + timedelta(seconds=300),
    )

    resp = await alice.post(f"/api/v1/agent/runs/{running.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    fresh = await db_session.get(AgentRun, running.id)
    assert fresh.status == "running"
    assert fresh.cancel_requested is True
    assert await _cancelled_events(db_session, running.id) == []


async def test_cancel_reviewing_run_marks_cancel_requested_only(
    agent_client_factory, db_session
) -> None:
    """reviewing Run：与 running 一致——只写 cancel_requested，Reviewer 返回后收口。"""
    alice, _ = await agent_client_factory("13600000094")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    now = utc_now()
    reviewing = await _add_run(
        db_session,
        user_id,
        session_id,
        status="reviewing",
        started_at=now,
        lease_owner="worker-a",
        lease_expires_at=now + timedelta(seconds=300),
    )

    resp = await alice.post(f"/api/v1/agent/runs/{reviewing.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reviewing"

    fresh = await db_session.get(AgentRun, reviewing.id)
    assert fresh.status == "reviewing"
    assert fresh.cancel_requested is True
    assert await _cancelled_events(db_session, reviewing.id) == []


async def test_resume_rejected_when_other_active_run_exists(
    agent_client_factory, db_session
) -> None:
    """resume 互斥（§5.5）：同 Session 已有其他活动 session_analyst_v1 Run
    （queued/running/reviewing）时，恢复 paused Run 返回 409 active_run_in_progress。
    """
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000095", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    paused = await _add_paused_run(db_session, user_id, session_id)
    await _add_run(db_session, user_id, session_id, status="running", started_at=utc_now())

    resp = await alice.post(f"/api/v1/agent/runs/{paused.id}/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "active_run_in_progress"
    # paused Run 未被恢复，执行器未被唤醒
    assert (await db_session.get(AgentRun, paused.id)).status == "paused"
    assert executor.submitted == []


async def test_run_under_archived_session_is_404(agent_client_factory) -> None:
    """§15.2：删除（软删除）Session 后，其下的 Run 一律 404，不泄露存在。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000053", executor=executor)
    session_id = await _create_session(alice)
    run_id = (await _post_message(alice, session_id, "分析")).json()["run_id"]

    assert (await alice.delete(f"/api/v1/agent/sessions/{session_id}")).status_code == 204

    assert (await alice.get(f"/api/v1/agent/runs/{run_id}")).status_code == 404
    assert (await alice.post(f"/api/v1/agent/runs/{run_id}/cancel")).status_code == 404
    assert (await alice.post(f"/api/v1/agent/runs/{run_id}/resume")).status_code == 404
    assert (await alice.get(f"/api/v1/agent/runs/{run_id}/events")).status_code == 404


async def test_events_sse_streams_and_honors_last_event_id(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, app = await agent_client_factory("13600000061", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    run_id = (await _post_message(alice, session_id, "分析")).json()["run_id"]

    # 预写事件：run.started(1) / thinking.delta(2) / run.completed(3)
    stream = AgentEventStream(db_session, app.state.agent_event_broker)
    await stream.append(run_id, user_id, "run.started", {})
    await stream.append(run_id, user_id, "thinking.delta", {"text": "思考中"})
    await stream.append(run_id, user_id, "run.completed", {})

    full = await alice.get(f"/api/v1/agent/runs/{run_id}/events")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    assert "id: 1" in full.text
    assert "id: 3" in full.text
    assert "event: run.completed" in full.text

    # Last-Event-ID 断线续传：只重放 seq>1
    resumed_body = await alice.get(
        f"/api/v1/agent/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
    )
    assert resumed_body.status_code == 200
    assert "id: 1" not in resumed_body.text
    assert "id: 2" in resumed_body.text
    assert "id: 3" in resumed_body.text

    # 归属失败 → 404（不泄露存在）
    bob, _ = await agent_client_factory("13600000062")
    assert (
        await bob.get(f"/api/v1/agent/runs/{run_id}/events")
    ).status_code == 404


async def test_internal_runs_excluded_from_session_detail(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000071", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    user_run_id = (await _post_message(alice, session_id, "分析")).json()["run_id"]

    # 直接插入一条内部 Review/Utility Run（同 Session、同 user，但 visibility=internal）
    now = utc_now()
    internal = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="internal",
        visibility="internal",
        profile_name="artifact_reviewer_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
    )
    db_session.add(internal)
    await db_session.flush()

    detail = await alice.get(f"/api/v1/agent/sessions/{session_id}")
    assert detail.status_code == 200
    run_ids = [run["id"] for run in detail.json()["runs"]]
    assert user_run_id in run_ids
    assert internal.id not in run_ids


async def test_session_detail_runs_ordered_by_created_at_not_random_id(
    agent_client_factory, db_session
) -> None:
    """runs 按 created_at 升序（id 仅作 tie-break）：前端取最后一个即最新 Run。

    旧实现按 AgentRun.id（uuid4 随机）排序，刷新后前端会锚定到任意历史 Run；
    这里让 id 字典序与 created_at 完全相反，锁定排序键是 created_at。
    kol_detail 辅助 Run 与用户主 Run 共用同一排序语义。
    """
    alice, _ = await agent_client_factory("13600000072")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)

    base = utc_now()

    async def insert_run(run_id: str, minutes: int, profile: str) -> None:
        db_session.add(
            AgentRun(
                id=run_id,
                session_id=session_id,
                user_id=user_id,
                run_kind="user",
                visibility="user",
                profile_name=profile,
                profile_version="v1",
                model="test-model",
                status="completed",
                decision_count=0,
                review_count=0,
                revision_count=0,
                created_at=base + timedelta(minutes=minutes),
                started_at=base + timedelta(minutes=minutes),
                completed_at=base + timedelta(minutes=minutes + 1),
            )
        )
        await db_session.flush()

    # id 字典序：newest < middle < oldest；created_at 顺序恰好相反。
    await insert_run("00000000-0000-4000-8000-0000000000a1", 2, "kol_detail_v1")
    await insert_run("7fffffff-ffff-4fff-bfff-ffffffffffff", 1, "session_analyst_v1")
    await insert_run("ffffffff-ffff-4fff-bfff-ffffffffffff", 0, "session_analyst_v1")

    detail = await alice.get(f"/api/v1/agent/sessions/{session_id}")
    assert detail.status_code == 200
    run_ids = [run["id"] for run in detail.json()["runs"]]
    assert run_ids == [
        "ffffffff-ffff-4fff-bfff-ffffffffffff",
        "7fffffff-ffff-4fff-bfff-ffffffffffff",
        "00000000-0000-4000-8000-0000000000a1",
    ]


# ---------------------------------------------------------------------------
# kol-details
# ---------------------------------------------------------------------------


async def test_kol_detail_idempotent_returns_existing_run(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory(
        "13600000081",
        kol_detail_service=KolDetailRunService(db_session, engine=None),
    )
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    existing = await _setup_active_kol_detail(db_session, user_id, session_id, "xiaohongshu", "k1")

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={"platform": "xiaohongshu", "kol_uid": "k1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["run_id"] == existing
    assert body["cached"] is False


async def test_kol_detail_rejects_client_profile_and_bad_ownership(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory(
        "13600000082",
        kol_detail_service=KolDetailRunService(db_session, engine=None),
    )
    bob, _ = await agent_client_factory("13600000083")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)

    # 客户端不能指定 Profile（extra=forbid）
    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={"platform": "xiaohongshu", "kol_uid": "k1", "profile": "session_analyst_v1"},
    )
    assert resp.status_code == 422

    # 归属失败 → 404（先为 k2 建活动 kol-detail Run，保证属主请求能走到响应）
    existing = await _setup_active_kol_detail(db_session, user_id, session_id, "xiaohongshu", "k2")
    owned = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={"platform": "xiaohongshu", "kol_uid": "k2"},
    )
    assert owned.status_code == 201
    assert owned.json()["run_id"] == existing
    foreign = await bob.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={"platform": "xiaohongshu", "kol_uid": "k2"},
    )
    assert foreign.status_code == 404


async def test_kol_detail_invalid_selection_ref_returns_404(
    agent_client_factory, db_session
) -> None:
    """selection 引用归属校验（§6.4）：不存在/跨 Session 的 selection_artifact_id
    统一 404，不泄漏资源存在性。"""
    alice, _ = await agent_client_factory(
        "13600000085",
        kol_detail_service=KolDetailRunService(db_session, engine=None),
    )
    session_id = await _create_session(alice)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={
            "platform": "xiaohongshu",
            "kol_uid": "k1",
            "selection_artifact_id": str(uuid4()),
            "selection_version": "1",
        },
    )
    assert resp.status_code == 404


async def test_kol_detail_coexists_with_active_session_analyst_run(
    agent_client_factory, db_session
) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory(
        "13600000084",
        executor=executor,
        kol_detail_service=KolDetailRunService(db_session, engine=None),
    )
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)

    # 先创建一个活动 session_analyst_v1 Run（queued）
    main = await _post_message(alice, session_id, "主分析")
    assert main.status_code == 201

    # kol-detail 与主 Run 可共存（并发车道独立）
    existing = await _setup_active_kol_detail(db_session, user_id, session_id, "douyin", "k9")
    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/kol-details",
        json={"platform": "douyin", "kol_uid": "k9"},
    )
    assert resp.status_code == 201
    assert resp.json()["run_id"] == existing


# ---------------------------------------------------------------------------
# 7. 取消待处理孤儿 Run 收口后的会话解锁（I1 集成断言）
# ---------------------------------------------------------------------------


async def test_cancel_orphan_settle_unblocks_new_message(
    agent_client_factory, db_session
) -> None:
    """崩溃残留的取消孤儿（running + cancel_requested + 过期租约）阻塞会话
    后续消息（409 active_run_in_progress）；恢复收口 cancelled 后，同一会话
    可正常创建新 Run——单活动 Run 约束不再被孤儿阻塞。"""
    from contextlib import asynccontextmanager

    from app.agent_runtime.executor import AgentRunExecutor

    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13600000095", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    now = utc_now()
    orphan = await _add_run(
        db_session,
        user_id,
        session_id,
        status="running",
        started_at=now,
        lease_owner="dead-worker",
        lease_expires_at=now - timedelta(seconds=10),
        cancel_requested=True,
    )

    # 孤儿仍算活动 Run：后续消息被单活动约束拒绝
    blocked = await _post_message(alice, session_id, "取消后我想继续分析")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "active_run_in_progress"

    # 恢复循环的取消收口路径（engine 不得被调用：孤儿不恢复模型执行）
    @asynccontextmanager
    async def _shared_session():
        yield db_session

    def engine_factory(db, worker_id, channel_permissions=()):
        raise AssertionError("cancel orphan must not execute engine")

    settle_executor = AgentRunExecutor(
        session_factory=_shared_session,
        engine_factory=engine_factory,
        worker_id="recovery-worker",
        claim_interval_seconds=0.01,
    )
    assert await settle_executor.settle_cancel_requested(orphan.id) is True
    fresh_orphan = await db_session.get(AgentRun, orphan.id)
    assert fresh_orphan.status == "cancelled"

    # 收口后会话解锁：新消息正常创建新 Run
    resp = await _post_message(alice, session_id, "取消后我想继续分析")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["run_id"] != orphan.id
    assert executor.submitted == [body["run_id"]]
    new_run = await db_session.get(AgentRun, body["run_id"])
    assert new_run is not None
    assert new_run.session_id == session_id


# ---------------------------------------------------------------------------
# 8. 父 Run / Artifact 引用 / retry（直接发布 Run 生命周期 Task 5）
# ---------------------------------------------------------------------------


async def _add_clarification_run(db_session, user_id: str, session_id: str) -> AgentRun:
    """clarification_requested 的 Run + 一条活动 pending_question 记忆。"""
    run = await _add_run(db_session, user_id, session_id, status="clarification_requested")
    db_session.add(
        MemoryEntry(
            id=str(uuid4()),
            session_id=session_id,
            source_run_id=run.id,
            memory_type="pending_question",
            content_json={"question": "分析哪个周期？", "options": ["近30天", "近90天"]},
            created_at=utc_now(),
        )
    )
    await db_session.flush()
    return run


async def _add_artifact_version(
    db_session,
    user_id: str,
    session_id: str,
    run_id: str,
    *,
    artifact_status: str = "published",
) -> AgentArtifactVersion:
    """创建 Artifact + Draft Revision + 一个 Version 行；artifact_status 控制发布态。"""
    now = utc_now()
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        module="brand",
        artifact_type="brand_report_v2",
        parent_artifact_id=None,
        artifact_key=f"brand/report-{uuid4().hex[:8]}",
        status=artifact_status,
        latest_version=1,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session_id,
        owner_run_id=run_id,
        current_revision=0,
        status="idle",
        review_count=0,
        revision_count=0,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()
    revision = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run_id,
        revision=0,
        schema_version="v2",
        payload_json={"title": "品牌报告"},
        payload_hash="h" * 64,
        created_at=now,
    )
    db_session.add(revision)
    await db_session.flush()
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=run_id,
        source_draft_revision_id=revision.id,
        schema_version="v2",
        payload_json={"title": "品牌报告"},
        data_status="complete",
        created_at=now,
    )
    db_session.add(version)
    await db_session.flush()
    return version


async def _add_evidence(
    db_session, session_id: str, run_id: str, *, availability: str = "available"
) -> str:
    """为 run 造一条 Evidence（attempt/step/call 链）；返回 evidence_id。

    attempt / step sequence 取当前最大值 +1，允许同一 run 挂多条 Evidence。
    """
    now = utc_now()
    attempt_no = (
        await db_session.scalar(
            select(func.max(AgentRunAttempt.attempt)).where(AgentRunAttempt.run_id == run_id)
        )
        or 0
    ) + 1
    step_sequence = (
        await db_session.scalar(
            select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run_id)
        )
        or 0
    ) + 1
    attempt = AgentRunAttempt(
        id=str(uuid4()), run_id=run_id, attempt=attempt_no, started_at=now, ended_at=now,
        decision_count=0, outcome="failed",
    )
    db_session.add(attempt)
    step = AgentStep(
        id=str(uuid4()), run_id=run_id, attempt_id=attempt.id, sequence=step_sequence,
        step_type="tool_call", status="settled", created_at=now,
    )
    db_session.add(step)
    call = AgentToolCall(
        id=str(uuid4()), run_id=run_id, step_id=step.id, logical_call_id=str(uuid4()),
        service="internal", internal_tool_name="seed", arguments_json={},
        arguments_hash="h" * 64, status="settled",
    )
    db_session.add(call)
    await db_session.flush()
    evidence = EvidenceItem(
        id=str(uuid4()), session_id=session_id, run_id=run_id, tool_call_id=call.id,
        source_type="mcp", source_name="seed", scope_json=None, period_json=None,
        raw_payload_json={"rows": []}, normalized_preview_json=None,
        payload_hash="h" * 64, collected_at=now, availability_status=availability,
    )
    db_session.add(evidence)
    await db_session.flush()
    return evidence.id


async def test_clarification_reply_creates_child_run(agent_client_factory, db_session) -> None:
    """显式 parent_run_id：澄清回答消息创建 Child Run，父链接落库并快照。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000001", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    clarification = await _add_clarification_run(db_session, user_id, session_id)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={
            "content": "选择近30天",
            "parent_run_id": clarification.id,
            "artifact_version_ids": [],
        },
    )
    assert resp.status_code == 201, resp.text
    run = await db_session.get(AgentRun, resp.json()["run_id"])
    assert run is not None
    assert run.id != clarification.id
    assert run.parent_run_id == clarification.id
    assert run.prompt_snapshot_json["parent_run_id"] == clarification.id
    assert executor.submitted == [run.id]


async def test_pending_question_reply_auto_links_parent(agent_client_factory, db_session) -> None:
    """未显式传 parent_run_id：活动 pending_question 的来源 Run 自动成为父 Run，
    且该 pending question 在回答建 Run 时被 supersede（不再影响后续消息）。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000002", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    clarification = await _add_clarification_run(db_session, user_id, session_id)

    resp = await _post_message(alice, session_id, "近30天")
    assert resp.status_code == 201, resp.text
    run = await db_session.get(AgentRun, resp.json()["run_id"])
    assert run.parent_run_id == clarification.id

    pendings = (
        await db_session.scalars(
            select(MemoryEntry).where(
                MemoryEntry.session_id == session_id,
                MemoryEntry.memory_type == "pending_question",
            )
        )
    ).all()
    assert len(pendings) == 1
    assert pendings[0].superseded_at is not None


async def test_parent_run_ownership_and_session_validated(
    agent_client_factory, db_session
) -> None:
    """parent Run 必须属本用户且同 Session：不存在/跨用户/跨 Session 统一 404。"""
    alice, _ = await agent_client_factory("13700000003")
    bob, _ = await agent_client_factory("13700000004")
    alice_session = await _create_session(alice)
    bob_session = await _create_session(bob)
    bob_id = await _me_id(bob)
    bob_run = await _add_run(db_session, bob_id, bob_session, status="completed")

    # 不存在的 parent
    missing = await alice.post(
        f"/api/v1/agent/sessions/{alice_session}/messages",
        json={"content": "继续", "parent_run_id": str(uuid4())},
    )
    assert missing.status_code == 404

    # 跨用户的 parent
    foreign = await alice.post(
        f"/api/v1/agent/sessions/{alice_session}/messages",
        json={"content": "继续", "parent_run_id": bob_run.id},
    )
    assert foreign.status_code == 404

    # 同用户但跨 Session 的 parent
    other_session = await _create_session(alice)
    alice_id = await _me_id(alice)
    other_run = await _add_run(db_session, alice_id, other_session, status="completed")
    cross_session = await alice.post(
        f"/api/v1/agent/sessions/{alice_session}/messages",
        json={"content": "继续", "parent_run_id": other_run.id},
    )
    assert cross_session.status_code == 404


async def test_cross_user_artifact_reference_is_404(agent_client_factory, db_session) -> None:
    """引用其他用户的 Artifact Version → 404，不泄漏存在性。"""
    alice, _ = await agent_client_factory("13700000005")
    bob, _ = await agent_client_factory("13700000006")
    alice_session = await _create_session(alice)
    bob_session = await _create_session(bob)
    bob_id = await _me_id(bob)
    bob_run = await _add_run(db_session, bob_id, bob_session, status="completed")
    foreign_version = await _add_artifact_version(db_session, bob_id, bob_session, bob_run.id)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{alice_session}/messages",
        json={"content": "基于这份报告分析", "artifact_version_ids": [foreign_version.id]},
    )
    assert resp.status_code == 404


async def test_unpublished_or_foreign_version_reference_is_404(
    agent_client_factory, db_session
) -> None:
    """草稿（未发布）产物与不存在的 Version 不可引用（§5.4）。"""
    alice, _ = await agent_client_factory("13700000007")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    run = await _add_run(db_session, user_id, session_id, status="completed")
    draft_version = await _add_artifact_version(
        db_session, user_id, session_id, run.id, artifact_status="draft"
    )

    for version_id in (draft_version.id, str(uuid4())):
        resp = await alice.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": "基于这份报告分析", "artifact_version_ids": [version_id]},
        )
        assert resp.status_code == 404


async def test_cross_session_published_version_reference_allowed(
    agent_client_factory, db_session
) -> None:
    """同用户跨 Session 的已发布 Version 可引用（§5.4 / §0 数据隔离：
    跨 Session 可复用当前用户的已发布 Artifact，草稿与其他用户数据除外）。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000007", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    run = await _add_run(db_session, user_id, session_id, status="completed")
    other_session = await _create_session(alice)
    other_version = await _add_artifact_version(db_session, user_id, other_session, run.id)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "基于这份报告分析", "artifact_version_ids": [other_version.id]},
    )
    assert resp.status_code == 201, resp.text
    new_run = await db_session.get(AgentRun, resp.json()["run_id"])
    assert new_run.prompt_snapshot_json["artifact_version_ids"] == [other_version.id]


async def test_artifact_reference_snapshot_frozen_into_run(
    agent_client_factory, db_session
) -> None:
    """合法引用：已发布 Version 可引用，引用快照写入新 Run 的 prompt_snapshot_json。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000008", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    source_run = await _add_run(db_session, user_id, session_id, status="completed")
    version = await _add_artifact_version(db_session, user_id, session_id, source_run.id)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "基于这份报告分析", "artifact_version_ids": [version.id]},
    )
    assert resp.status_code == 201, resp.text
    run = await db_session.get(AgentRun, resp.json()["run_id"])
    assert run.prompt_snapshot_json["artifact_version_ids"] == [version.id]


async def test_retry_creates_new_run_without_reopening_failed_run(
    agent_client_factory, db_session
) -> None:
    """retry failed Run：创建新的 user-visible Child Run（parent 指向原 Run），
    继承原 Run 的输入引用（父链 / 输入 Version / 冻结 Evidence）并冻结仍有效的
    产出（available Evidence / 已发布 Version）；失效引用（过期 Evidence、未发布
    Version、不存在的 ID）被重新验证丢弃；原 Run 保持 failed 不被重开。"""
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000009", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    parent = await _add_run(db_session, user_id, session_id, status="completed")
    # 输入引用：真实存在的已发布 Version 与 available Evidence。
    input_version = await _add_artifact_version(db_session, user_id, session_id, parent.id)
    input_evidence = await _add_evidence(
        db_session, session_id, parent.id, availability="available"
    )
    # 失效引用：expired Evidence、draft（未发布）Version、不存在的 ID。
    expired_input = await _add_evidence(
        db_session, session_id, parent.id, availability="expired"
    )
    draft_version = await _add_artifact_version(
        db_session, user_id, session_id, parent.id, artifact_status="draft"
    )
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        error_code="ALL_ARTIFACTS_FAILED",
        completed_at=utc_now(),
        parent_run_id=parent.id,
        prompt_snapshot_json={
            "parent_run_id": parent.id,
            "artifact_version_ids": [input_version.id, draft_version.id, "ghost-version"],
            "evidence_ids": [input_evidence, expired_input, "ghost-evidence"],
        },
    )
    available = await _add_evidence(db_session, session_id, failed.id, availability="available")
    await _add_evidence(db_session, session_id, failed.id, availability="expired")
    version = await _add_artifact_version(db_session, user_id, session_id, failed.id)

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    assert retried is not None
    assert retried.id != failed.id
    assert retried.parent_run_id == failed.id
    assert retried.status == "queued"
    assert retried.visibility == "user"
    assert retried.profile_name == failed.profile_name
    assert executor.submitted == [retried.id]

    # 冻结的引用快照：仍有效的输入引用 + 产出引用，去重保序；失效引用被丢弃。
    snapshot = retried.prompt_snapshot_json
    assert snapshot["retry_of"] == failed.id
    assert snapshot["parent_run_id"] == parent.id
    assert snapshot["evidence_ids"] == [input_evidence, available]
    assert "expired_input" not in snapshot["evidence_ids"]
    assert "ghost-evidence" not in snapshot["evidence_ids"]
    assert snapshot["artifact_version_ids"] == [input_version.id, version.id]
    assert draft_version.id not in snapshot["artifact_version_ids"]
    assert "ghost-version" not in snapshot["artifact_version_ids"]

    # 原 Run 绝不修改或重开。
    original = await db_session.get(AgentRun, failed.id)
    assert original.status == "failed"
    assert original.error_code == "ALL_ARTIFACTS_FAILED"


async def test_retry_accepts_paused_run(agent_client_factory, db_session) -> None:
    executor = FakeExecutor()
    alice, _ = await agent_client_factory("13700000010", executor=executor)
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    paused = await _add_paused_run(db_session, user_id, session_id)

    resp = await alice.post(f"/api/v1/agent/runs/{paused.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    assert retried.parent_run_id == paused.id
    assert (await db_session.get(AgentRun, paused.id)).status == "paused"


async def test_retry_rejects_non_terminal_run(agent_client_factory, db_session) -> None:
    """retry 只接受 failed/paused：queued/completed/cancelled 一律 409。"""
    alice, _ = await agent_client_factory("13700000011")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    for status_value in ("queued", "completed", "cancelled"):
        run = await _add_run(db_session, user_id, session_id, status=status_value)
        resp = await alice.post(f"/api/v1/agent/runs/{run.id}/retry")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "run_not_retryable"


async def test_retry_cross_user_is_404(agent_client_factory, db_session) -> None:
    alice, _ = await agent_client_factory("13700000012")
    bob, _ = await agent_client_factory("13700000013")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    failed = await _add_run(db_session, user_id, session_id, status="failed")

    assert (await bob.post(f"/api/v1/agent/runs/{failed.id}/retry")).status_code == 404


async def test_retry_blocked_by_other_active_run(agent_client_factory, db_session) -> None:
    """retry 遵守单活动主 Run 约束：同 Session 已有活动 Run 时 409。"""
    alice, _ = await agent_client_factory("13700000014")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    failed = await _add_run(db_session, user_id, session_id, status="failed")
    await _add_run(db_session, user_id, session_id, status="running", started_at=utc_now())

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "active_run_in_progress"


async def test_retry_rejects_non_session_analyst_profile(
    agent_client_factory, db_session
) -> None:
    """retry 仅限 session_analyst_v1 主 Run：kol_detail_v1 辅助 Run 即使
    failed 也不可经此端点重试（409 run_not_retryable）。

    kol_detail Run 的触发上下文在 KOL_DETAIL_SNAPSHOT_KEY（platform/kol_uid），
    且无 input_message_id——整体覆盖 prompt_snapshot_json 的 retry 会让
    transcript 回退到会话最近一条用户消息，锚定错误意图；重试必须走
    KolDetailRunService 自己的缓存/回退车道。
    """
    alice, _ = await agent_client_factory("13700000015")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    kol_detail_run = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        profile_name="kol_detail_v1",
    )

    resp = await alice.post(f"/api/v1/agent/runs/{kol_detail_run.id}/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "run_not_retryable"
    # 原 Run 不受影响，且未创建任何 Child Run。
    assert (await db_session.get(AgentRun, kol_detail_run.id)).status == "failed"
    children = (
        await db_session.scalars(
            select(AgentRun).where(AgentRun.parent_run_id == kol_detail_run.id)
        )
    ).all()
    assert children == []


# ---------------------------------------------------------------------------
# messages upload_ids：引用本 Session 的 parsed 上传，写入 Run 快照
# ---------------------------------------------------------------------------


async def _seed_upload(
    db_session, user_id: str, session_id: str, *, status: str = "parsed"
) -> str:
    from app.agent_runtime.models import AgentUpload, EvidenceItem

    upload = AgentUpload(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session_id,
        original_filename="投放数据.csv",
        mime_type="text/csv",
        size_bytes=100,
        sha256="b" * 64,
        storage_key=f"{user_id}/{uuid4()}.csv",
        status=status,
        created_at=utc_now(),
        completed_at=utc_now() if status == "parsed" else None,
    )
    db_session.add(upload)
    await db_session.flush()
    if status == "parsed":
        evidence = EvidenceItem(
            id=str(uuid4()),
            session_id=session_id,
            run_id=None,
            tool_call_id=None,
            upload_id=upload.id,
            source_type="user_upload",
            source_name="user_upload",
            raw_payload_json={"columns": ["平台"], "rows": []},
            normalized_preview_json={"preview": {}, "row_count": 0, "truncated": False},
            payload_hash="b" * 64,
            collected_at=utc_now(),
            availability_status="available",
        )
        db_session.add(evidence)
        await db_session.flush()
    return upload.id


async def _seed_upload_with_evidences(
    db_session, user_id: str, session_id: str, *, evidences: list[tuple[str, str]]
) -> str:
    """造一个 upload + 多条 upload Evidence；evidences 为 (availability, collected_at) 列表。

    返回 upload_id。用于"不可用 Evidence 不可冻结 / reparse 后取最新 available"。
    """
    from app.agent_runtime.models import AgentUpload, EvidenceItem

    upload_id = str(uuid4())
    db_session.add(
        AgentUpload(
            id=upload_id,
            user_id=user_id,
            session_id=session_id,
            original_filename="投放数据.csv",
            mime_type="text/csv",
            size_bytes=100,
            sha256="c" * 64,
            storage_key=f"{user_id}/{uuid4()}.csv",
            status="parsed",
            created_at=utc_now(),
            completed_at=utc_now(),
        )
    )
    await db_session.flush()
    for availability, collected_at in evidences:
        db_session.add(
            EvidenceItem(
                id=str(uuid4()),
                session_id=session_id,
                run_id=None,
                tool_call_id=None,
                upload_id=upload_id,
                source_type="user_upload",
                source_name="user_upload",
                raw_payload_json={"columns": ["平台"], "rows": []},
                normalized_preview_json={"preview": {}, "row_count": 0, "truncated": False},
                payload_hash="c" * 64,
                collected_at=collected_at,
                availability_status=availability,
            )
        )
    await db_session.flush()
    return upload_id


async def test_message_with_upload_ids_writes_snapshot(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory("13600000101")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    upload_id = await _seed_upload(db_session, user_id, session_id)

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析这份投放数据", "upload_ids": [upload_id]},
    )
    assert resp.status_code == 201, resp.text
    run = await db_session.get(AgentRun, resp.json()["run_id"])
    assert run is not None
    assert "upload_refs" in (run.prompt_snapshot_json or {})
    refs = (run.prompt_snapshot_json or {})["upload_refs"]
    assert len(refs) == 1
    assert refs[0]["upload_id"] == upload_id
    assert "evidence_id" in refs[0]
    assert refs[0]["filename"] == "投放数据.csv"


async def test_message_upload_ids_must_belong_to_session(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory("13600000102")
    bob, _ = await agent_client_factory("13600000103")
    session_a = await _create_session(alice)
    session_b = await _create_session(bob)
    user_a = await _me_id(alice)
    user_b = await _me_id(bob)

    # 跨用户：B 的 upload 引用进 A 的消息 → 404（归属失败不泄漏）。
    upload_b = await _seed_upload(db_session, user_b, session_b)
    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_a}/messages",
        json={"content": "分析", "upload_ids": [upload_b]},
    )
    assert resp.status_code == 404, resp.text

    # 跨 Session：A 自己的 upload 引用进 B 的 Session → 404。
    upload_a = await _seed_upload(db_session, user_a, session_a)
    resp = await bob.post(
        f"/api/v1/agent/sessions/{session_b}/messages",
        json={"content": "分析", "upload_ids": [upload_a]},
    )
    assert resp.status_code == 404, resp.text


async def test_message_upload_ids_must_be_parsed(
    agent_client_factory, db_session
) -> None:
    alice, _ = await agent_client_factory("13600000104")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    upload_id = await _seed_upload(db_session, user_id, session_id, status="failed")

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析", "upload_ids": [upload_id]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "upload_not_available"


async def test_message_upload_ids_at_most_ten(agent_client_factory, db_session) -> None:
    alice, _ = await agent_client_factory("13600000105")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    upload_ids = [
        await _seed_upload(db_session, user_id, session_id) for _ in range(11)
    ]

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析", "upload_ids": upload_ids},
    )
    assert resp.status_code == 422, resp.text


async def test_message_upload_ids_participate_in_idempotency_hash(
    agent_client_factory, db_session
) -> None:
    """幂等哈希包含 upload_ids：同文本不同上传不误复用同一 Run（Gate A 教训）。"""
    alice, _ = await agent_client_factory("13600000106")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    upload_a = await _seed_upload(db_session, user_id, session_id)
    upload_b = await _seed_upload(db_session, user_id, session_id)

    first = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析这份数据", "upload_ids": [upload_a]},
        headers={"Idempotency-Key": "same-key"},
    )
    assert first.status_code == 201, first.text
    # 同 key 同 payload → 复用；同 key 不同 upload_ids → 409 payload mismatch。
    reused = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析这份数据", "upload_ids": [upload_a]},
        headers={"Idempotency-Key": "same-key"},
    )
    assert reused.status_code == 201
    assert reused.json()["reused"] is True

    conflict = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析这份数据", "upload_ids": [upload_b]},
        headers={"Idempotency-Key": "same-key"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_payload_mismatch"


# ---------------------------------------------------------------------------
# P1-3: upload_refs 重验证（availability 过滤 / 最新可选 / retry 逐条剔除）
# ---------------------------------------------------------------------------


async def test_message_upload_unavailable_evidence_is_404(
    agent_client_factory, db_session
) -> None:
    """首次创建时 upload Evidence 非 available 不可冻结 → 404。"""
    alice, _ = await agent_client_factory("13600000201")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    upload_id = await _seed_upload_with_evidences(
        db_session, user_id, session_id,
        evidences=[("expired", utc_now())],
    )

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析", "upload_ids": [upload_id]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] in ("upload_evidence_missing", "upload_not_available")


async def test_upload_reparse_selects_latest_available_evidence(
    agent_client_factory, db_session
) -> None:
    """reparse（append-only）后冻结应选最新 available Evidence，不任意选旧行。"""
    alice, _ = await agent_client_factory("13600000202")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    base = utc_now()
    upload_id = await _seed_upload_with_evidences(
        db_session, user_id, session_id,
        evidences=[
            ("available", base),  # 旧 available
            ("expired", base),  # 旧 unavailable
            ("available", base + timedelta(seconds=1)),  # 最新 available
        ],
    )

    resp = await alice.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "分析", "upload_ids": [upload_id]},
    )
    assert resp.status_code == 201, resp.text
    run = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (run.prompt_snapshot_json or {})["upload_refs"]
    assert len(refs) == 1
    # 取最新 available Evidence（collected_at 最大者）。
    evidence = await db_session.scalar(
        select(EvidenceItem)
        .where(EvidenceItem.upload_id == upload_id)
        .order_by(EvidenceItem.collected_at.desc(), EvidenceItem.id.desc())
        .limit(1)
    )
    assert evidence is not None
    assert evidence.availability_status == "available"
    assert refs[0]["evidence_id"] == evidence.id


async def test_retry_drops_invalid_upload_ref_keeps_valid(
    agent_client_factory, db_session
) -> None:
    """retry 中一个失效 upload 引用被剔除，其他有效引用保留（不整体清空）。"""
    alice, _ = await agent_client_factory("13600000203")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    valid_upload = await _seed_upload(db_session, user_id, session_id)
    invalid_upload = await _seed_upload_with_evidences(
        db_session, user_id, session_id, evidences=[("expired", utc_now())]
    )
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={
            "upload_refs": [
                {"upload_id": valid_upload, "evidence_id": "e1", "filename": "a.csv", "sha256": "a"},
                {"upload_id": invalid_upload, "evidence_id": "e2", "filename": "b.csv", "sha256": "b"},
            ]
        },
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    assert refs is not None
    ids = [ref["upload_id"] for ref in refs]
    assert valid_upload in ids
    assert invalid_upload not in ids


async def test_retry_corrupt_upload_snapshot_no_500(
    agent_client_factory, db_session
) -> None:
    """retry 遇到损坏快照（非 dict / 缺 upload_id）只剔除，不抛 KeyError/500。"""
    alice, _ = await agent_client_factory("13600000204")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    valid_upload = await _seed_upload(db_session, user_id, session_id)
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={
            "upload_refs": [
                {"upload_id": valid_upload, "evidence_id": "e1", "filename": "a.csv", "sha256": "a"},
                "corrupt-string",
                {"bad": 123},
                None,
                {"upload_id": ""},
            ]
        },
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    assert refs is not None
    assert [ref["upload_id"] for ref in refs] == [valid_upload]


async def test_retry_upload_refs_order_stable(agent_client_factory, db_session) -> None:
    """retry 生成的新 upload_refs 顺序稳定（按入参首次出现顺序，去重）。"""
    alice, _ = await agent_client_factory("13600000205")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    u2 = await _seed_upload(db_session, user_id, session_id)
    u1 = await _seed_upload(db_session, user_id, session_id)
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={
            "upload_refs": [
                {"upload_id": u2, "evidence_id": "e2", "filename": "b.csv", "sha256": "b"},
                {"upload_id": u1, "evidence_id": "e1", "filename": "a.csv", "sha256": "a"},
                {"upload_id": u2, "evidence_id": "e2", "filename": "b.csv", "sha256": "b"},
            ]
        },
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    assert [ref["upload_id"] for ref in refs] == [u2, u1]


async def test_retry_context_only_frozen_evidence(agent_client_factory, db_session) -> None:
    """Context 只能看到新快照中冻结的 Evidence（不混入失效 upload 的 Evidence）。"""
    from app.agent_runtime.context import SessionContextBuilder
    from app.agent_runtime.tools.registry import ToolRegistry

    alice, _ = await agent_client_factory("13600000206")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    valid_upload = await _seed_upload(db_session, user_id, session_id)
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={
            "upload_refs": [
                {"upload_id": valid_upload, "evidence_id": "e1", "filename": "a.csv", "sha256": "a"},
            ]
        },
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    assert refs is not None
    frozen_evidence_ids = {ref["evidence_id"] for ref in refs}

    builder = SessionContextBuilder(db_session, ToolRegistry())
    references = await builder._run_references(retried)
    context_refs = references.get("upload_refs") or []
    assert context_refs
    # Context 只含新快照冻结的 Evidence。
    assert {ref["evidence_id"] for ref in context_refs} == frozen_evidence_ids


# ---------------------------------------------------------------------------
# P2: upload_refs 容器本身校验（非 list 不 500，合法元素保留）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_container",
    [
        123,
        {},
        "bad",
        None,
    ],
)
async def test_retry_upload_refs_non_list_container_no_500(
    agent_client_factory, db_session, bad_container
) -> None:
    """upload_refs 为 123/{}/'bad'/null 时 retry 不抛 500，合法上传仍冻结。"""
    alice, _ = await agent_client_factory("13600000301")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={"upload_refs": bad_container},
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    # 非 list 容器被剔除，但合法上传我们单独验证（见下测）；此处无有效引用则不冻结。
    assert refs is None or refs == []


async def test_retry_upload_refs_mixed_damaged_and_valid_container(
    agent_client_factory, db_session
) -> None:
    """upload_refs=[损坏元素, 合法元素] 不 500，合法元素保留。"""
    alice, _ = await agent_client_factory("13600000302")
    session_id = await _create_session(alice)
    user_id = await _me_id(alice)
    valid_upload = await _seed_upload(db_session, user_id, session_id)
    failed = await _add_run(
        db_session,
        user_id,
        session_id,
        status="failed",
        prompt_snapshot_json={
            "upload_refs": [
                "corrupt-str",
                {"bad": 1},
                None,
                {"upload_id": valid_upload, "evidence_id": "e1", "filename": "a.csv", "sha256": "a"},
            ]
        },
    )

    resp = await alice.post(f"/api/v1/agent/runs/{failed.id}/retry")
    assert resp.status_code == 201, resp.text
    retried = await db_session.get(AgentRun, resp.json()["run_id"])
    refs = (retried.prompt_snapshot_json or {}).get("upload_refs")
    assert refs is not None
    assert [ref["upload_id"] for ref in refs] == [valid_upload]
