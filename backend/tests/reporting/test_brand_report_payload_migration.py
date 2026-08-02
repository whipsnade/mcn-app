"""迁移 0026：analysis_reports 增加 payload_json / template_version 两列。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.session import SessionFactory
from app.reporting.models import AnalysisReport


BACKEND_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_HEAD = "0027_agent_runtime_v3"


def head_revision() -> str:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    revision = ScriptDirectory.from_config(config).get_current_head()
    assert revision is not None
    return revision


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


async def payload_columns() -> set[str]:
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'analysis_reports' "
                    "AND COLUMN_NAME IN ('payload_json', 'template_version')"
                )
            )
        ).all()
    return {row[0] for row in rows}


def test_alembic_head_is_0027() -> None:
    assert head_revision() == EXPECTED_HEAD


@pytest.mark.asyncio
async def test_0026_upgrade_downgrade_upgrade_column_lifecycle() -> None:
    try:
        return_code, output = await run_alembic("upgrade", "head")
        assert return_code == 0, output
        assert await payload_columns() == {"payload_json", "template_version"}

        # 回退到 0026 前一版，确保 0026 的 downgrade 执行并移除 payload 列。
        return_code, output = await run_alembic("downgrade", "0025_kol_selection_detail_views")
        assert return_code == 0, output
        assert await payload_columns() == set()

        return_code, output = await run_alembic("upgrade", "head")
        assert return_code == 0, output
        assert await payload_columns() == {"payload_json", "template_version"}
    finally:
        await run_alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_analysis_report_payload_orm_roundtrip(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000101")
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    session_id = created.json()["id"]

    now = datetime.now(UTC).replace(tzinfo=None)
    report_id = str(uuid4())
    db_session.add(
        AnalysisReport(
            id=report_id,
            session_id=session_id,
            report_type="brand_analysis",
            version=1,
            title="品牌报告",
            blocks_json=[],
            status="completed",
            payload_json={"a": 1},
            template_version="brand_report_v2",
            created_at=now,
            updated_at=now,
        )
    )
    legacy_id = str(uuid4())
    db_session.add(
        AnalysisReport(
            id=legacy_id,
            session_id=session_id,
            report_type="kol_analysis",
            version=1,
            title="旧式报告",
            blocks_json=[],
            status="completed",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    db_session.expire_all()

    report = await db_session.get(AnalysisReport, report_id)
    assert report is not None
    assert report.payload_json == {"a": 1}
    assert report.template_version == "brand_report_v2"

    legacy = await db_session.get(AnalysisReport, legacy_id)
    assert legacy is not None
    assert legacy.payload_json is None
    assert legacy.template_version is None
