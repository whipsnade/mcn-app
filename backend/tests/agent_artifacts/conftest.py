"""agent_artifacts 测试共享 fixtures：AgentSession / AgentRun / 遗留 sessions 行。

``legacy_session_factory`` 插入遗留 ``sessions`` 行，因为
``artifact_read_states.session_id`` 的 FK 目标仍是旧 ``sessions.id``（新列与旧列
在同一张表共存，设计 §8.1）。其余 Draft/事件 FK 目标都是 agent_sessions。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun, AgentSession


@pytest_asyncio.fixture
async def session_factory(db_session: AsyncSession):
    """创建一条 agent_sessions 行。"""

    async def create_session(user_id: str) -> AgentSession:
        now = datetime.now(UTC).replace(tzinfo=None)
        session = AgentSession(
            id=str(uuid4()),
            user_id=user_id,
            title="测试会话",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db_session.add(session)
        await db_session.flush()
        return session

    return create_session


@pytest_asyncio.fixture
async def run_factory(db_session: AsyncSession):
    """创建一条 agent_runs 行（运行中的用户 Run）。"""

    async def create_run(session_id: str, user_id: str) -> AgentRun:
        now = datetime.now(UTC).replace(tzinfo=None)
        run = AgentRun(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            run_kind="user",
            visibility="user",
            profile_name="kol_analyst_v1",
            profile_version="v1",
            model="test-model",
            status="running",
            started_at=now,
        )
        db_session.add(run)
        await db_session.flush()
        return run

    return create_run


@pytest_asyncio.fixture
async def legacy_session_factory(db_session: AsyncSession):
    """插入一条遗留 sessions 行（artifact_read_states.session_id FK 目标）。

    使用与 agent_session 相同的 id 字符串，让 read_states 与事件/Artifact 的
    session FK 同时成立。
    """

    async def create_legacy_session(user_id: str, session_id: str) -> str:
        now = datetime.now(UTC).replace(tzinfo=None)
        await db_session.execute(
            text(
                "INSERT INTO sessions "
                "(id, user_id, title, brand, status, platforms, target_audience, "
                "filters_snapshot, is_starred, last_accessed_at, created_at, updated_at) "
                "VALUES (:id, :uid, :title, :brand, :status, :platforms, :audience, "
                ":filters, 0, :now, :now, :now)"
            ),
            {
                "id": session_id,
                "uid": user_id,
                "title": "测试会话",
                "brand": "瑞幸",
                "status": "active",
                "platforms": "[]",
                "audience": "",
                "filters": "{}",
                "now": now,
            },
        )
        return session_id

    return create_legacy_session
