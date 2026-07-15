import asyncio
import os
from pathlib import Path
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from app.db.session import SessionFactory
from app.identity.models import User
from app.workspace.models import Message, WorkspaceSession


BACKEND_ROOT = Path(__file__).resolve().parents[2]


async def run_alembic(*arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        cwd=BACKEND_ROOT,
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode()


async def migration_state(message_id: str) -> tuple[str, str, str, int]:
    async with SessionFactory() as session:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        data_type = await session.scalar(
            text(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'messages' AND COLUMN_NAME = 'content'"
            )
        )
        row = (
            await session.execute(
                select(Message.content, func.octet_length(Message.content)).where(
                    Message.id == message_id
                )
            )
        ).one()
    return version, data_type, row[0], row[1]


@pytest.mark.asyncio
async def test_0004_downgrade_preflights_oversized_message_without_truncation() -> None:
    return_code, output = await run_alembic("upgrade", "head")
    assert return_code == 0, output

    now = datetime.now(UTC).replace(tzinfo=None)
    user_id = str(uuid4())
    workspace_id = str(uuid4())
    message_id = str(uuid4())
    oversized_content = "😀" * 20_000
    try:
        async with SessionFactory.begin() as setup:
            setup.add(
                User(
                    id=user_id,
                    nickname="迁移边界测试",
                    role="user",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            await setup.flush()
            setup.add(
                WorkspaceSession(
                    id=workspace_id,
                    user_id=user_id,
                    title="迁移边界测试",
                    brand="测试品牌",
                    campaign_name="迁移边界测试",
                    status="draft",
                    platforms=["bilibili"],
                    category="科技",
                    target_audience="科技兴趣用户",
                    budget_min=None,
                    budget_max=None,
                    filters_snapshot={},
                    is_starred=False,
                    last_accessed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await setup.flush()
            setup.add(
                Message(
                    id=message_id,
                    session_id=workspace_id,
                    user_id=user_id,
                    role="user",
                    content=oversized_content,
                    sequence=1,
                    metadata_json={},
                    created_at=now,
                )
            )

        return_code, output = await run_alembic("downgrade", "0003")

        assert return_code != 0
        version, data_type, stored_content, stored_bytes = await migration_state(message_id)
        assert version == "0004"
        assert data_type == "mediumtext"
        assert stored_content == oversized_content
        assert stored_bytes == 80_000
        assert "message_content_exceeds_text_limit" in output

        async with SessionFactory.begin() as shorten:
            await shorten.execute(
                update(Message).where(Message.id == message_id).values(content="已缩短")
            )
        return_code, output = await run_alembic("downgrade", "0003")
        assert return_code == 0, output
        version, data_type, stored_content, stored_bytes = await migration_state(message_id)
        assert (version, data_type, stored_content, stored_bytes) == (
            "0003",
            "text",
            "已缩短",
            9,
        )

        return_code, output = await run_alembic("upgrade", "head")
        assert return_code == 0, output
        version, data_type, stored_content, stored_bytes = await migration_state(message_id)
        assert (version, data_type, stored_content, stored_bytes) == (
            "0005",
            "mediumtext",
            "已缩短",
            9,
        )
    finally:
        await run_alembic("upgrade", "head")
        async with SessionFactory.begin() as cleanup:
            await cleanup.execute(delete(Message).where(Message.id == message_id))
            await cleanup.execute(
                delete(WorkspaceSession).where(WorkspaceSession.id == workspace_id)
            )
            await cleanup.execute(delete(User).where(User.id == user_id))
