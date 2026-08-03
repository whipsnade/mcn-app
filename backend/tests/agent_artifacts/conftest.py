"""agent_artifacts 测试共享 fixtures：AgentSession / AgentRun。

已读水位自迁移 0028 起写入独立的 ``agent_artifact_read_states``
（session FK → agent_sessions），不再需要往遗留 ``sessions`` 表插同 id 行兜底。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
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
