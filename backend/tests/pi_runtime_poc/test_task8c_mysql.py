"""Task 8C：真实 POC MySQL 上的 fixture 持久化顺序回归。"""

import os

import pytest
from sqlalchemy import delete, select

from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession
from app.billing.models import Wallet
from app.core.config import get_settings
from app.db.session import SessionFactory
from app.identity.models import User, UserChannelPermission
from app.pi_runtime_poc.comparison import PocCase, PocCaseFactory

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PI_POC_MYSQL_TESTS") != "1",
    reason="仅在显式隔离 kol_insight_pi_poc MySQL 验收中执行",
)


async def test_case_factory_commits_session_run_and_message_in_mysql_poc_order() -> None:
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8c-mysql-order",
        user_question="只验证 POC fixture 持久化顺序。",
        date_anchor="2026-08-07",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8c-mysql",
        model_name=settings.tencent_plan_model,
    )
    user_id: str | None = None
    run_id: str | None = None

    try:
        run_id = await factory.create(case, "current")
        async with SessionFactory() as db:
            run = await db.get(AgentRun, run_id)
            assert run is not None
            user_id = run.user_id
            message = await db.scalar(select(AgentMessage).where(AgentMessage.id == run.input_message_id))
            assert message is not None
            assert message.session_id == run.session_id
            assert await db.get(AgentSession, run.session_id) is not None
            assert await db.get(Wallet, user_id) is not None
            assert (
                await db.scalar(
                    select(UserChannelPermission.id).where(UserChannelPermission.user_id == user_id)
                )
                is not None
            )
    finally:
        if user_id is not None and run_id is not None:
            async with SessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    run.input_message_id = None
                    await db.flush()
                    await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
                    await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
                    await db.execute(delete(AgentSession).where(AgentSession.user_id == user_id))
                await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id == user_id))
                await db.execute(delete(Wallet).where(Wallet.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
