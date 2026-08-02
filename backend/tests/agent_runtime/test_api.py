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

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.agent_artifacts.models import AgentArtifact, ArtifactDraft
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.kol_detail import KolDetailRunService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentRunAttempt, AgentSession
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
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
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
