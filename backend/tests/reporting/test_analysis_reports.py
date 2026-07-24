import pytest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import engine
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import HeadingBlock, MarkdownBlock, MetricGridBlock, MetricItem, ReportDocument
from app.reporting.models import AnalysisReport
from app.tasks.models import AnalysisTask, TaskEvent
from app.workspace.models import Message, WorkspaceSession


def _document() -> ReportDocument:
    return ReportDocument(
        title="美妆行业分析",
        conclusion="行业保持增长。",
        blocks=[
            HeadingBlock(text="整体热度"),
            MetricGridBlock(
                items=[MetricItem(label="总声量", value=4373, unit="万帖", delta="+37.2%")]
            ),
            MarkdownBlock(text="同比大幅扩张但环比回落。"),
        ],
    )


async def _create_agent_session(client, suffix: str) -> tuple[str, str]:
    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "",
            "campaign_name": None,
            "platforms": [],
            "category": "美妆",
            "target_audience": "",
            "initial_query": f"分析美妆行业在社交媒体的讨论热度和发展趋势 {suffix}",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["latest_task"]["kind"] == "agent"
    return payload["id"], payload["latest_task"]["id"]


@pytest.mark.asyncio
async def test_agent_task_report_build_is_idempotent_and_emits_event(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000041")
    session_id, task_id = await _create_agent_session(client, "build")

    service = AnalysisReportService(db_session)
    report = await service.build(task_id, document=_document())

    assert report.version == 1
    assert report.session_id == session_id
    assert report.blocks_json[0]["type"] == "heading"

    again = await service.build(task_id, document=_document())
    assert again.id == report.id

    events = list(
        (
            await db_session.scalars(
                select(TaskEvent).where(
                    TaskEvent.task_id == task_id,
                    TaskEvent.event_type == "report.updated",
                )
            )
        ).all()
    )
    assert len(events) == 1
    assert events[0].payload_json["report_id"] == report.id


@pytest.mark.asyncio
async def test_analysis_report_read_and_session_summary(auth_client_factory, db_session) -> None:
    owner = await auth_client_factory("13400000043")
    session_id, task_id = await _create_agent_session(owner, "read")

    service = AnalysisReportService(db_session)
    report = await service.build(task_id, document=_document())

    response = await owner.get(f"/api/v1/analysis-reports/{report.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "美妆行业分析"
    assert payload["blocks"][1]["type"] == "metric_grid"

    other = await auth_client_factory("13400000044")
    forbidden = await other.get(f"/api/v1/analysis-reports/{report.id}")
    assert forbidden.status_code == 404

    session_response = await owner.get(f"/api/v1/sessions/{session_id}")
    assert session_response.status_code == 200
    summary = session_response.json()["latest_analysis_report"]
    assert summary is not None
    assert summary["id"] == report.id
    assert summary["version"] == 1


@pytest.mark.asyncio
async def test_build_versions_increment_per_session(auth_client_factory, db_session) -> None:
    """迁移 0020 后 version 按会话编号：同一会话第二个任务的报告 version=2。"""
    client = await auth_client_factory("13400000045")
    session_id, task_id = await _create_agent_session(client, "version")

    service = AnalysisReportService(db_session)
    first = await service.build(task_id, document=_document())
    assert first.version == 1

    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    next_sequence = (
        await db_session.scalar(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        )
        or 0
    ) + 1
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=source.user_id,
        role="user",
        content="继续分析",
        sequence=next_sequence,
        metadata_json={},
        created_at=now,
    )
    second_task = AnalysisTask(
        id=str(uuid4()),
        user_id=source.user_id,
        session_id=session_id,
        trigger_message_id=message.id,
        kind="agent",
        status="queued",
        max_calls=10,
        estimated_points=0,
        creation_order=2,
        created_at=now,
        updated_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(second_task)
    await db_session.flush()

    second = await service.build(second_task.id, document=_document())
    assert second.version == 2
    assert second.session_id == session_id
    assert second.task_id == second_task.id


@pytest.mark.asyncio
async def test_build_session_report_increments_version(auth_client_factory, db_session) -> None:
    """会话级报告不幂等：每次点击生成新版本，task_id 为 NULL。"""
    client = await auth_client_factory("13400000046")
    session_id, task_id = await _create_agent_session(client, "session-report")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    first = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    assert first.version == 1
    assert first.session_id == session_id
    assert first.task_id is None
    assert first.status == "completed"

    second = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    assert second.version == 2
    assert second.id != first.id
    assert second.task_id is None

    latest = await service.latest_session_report(session_id)
    assert latest is not None
    assert latest.id == second.id
    assert latest.version == 2


@pytest.mark.asyncio
async def test_build_session_report_rejects_foreign_session(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000047")
    session_id, _ = await _create_agent_session(client, "foreign-session")

    service = AnalysisReportService(db_session)
    with pytest.raises(LookupError, match="session_not_found"):
        await service.build_session_report(
            user_id=str(uuid4()), session_id=session_id, document=_document()
        )


@pytest.mark.asyncio
async def test_session_report_read_and_session_dto(auth_client_factory, db_session) -> None:
    """task_id 为 NULL 的会话级报告：owner 可读、他人 404、会话 DTO task_id 为 null。"""
    owner = await auth_client_factory("13400000048")
    session_id, task_id = await _create_agent_session(owner, "session-dto")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    report = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    response = await owner.get(f"/api/v1/analysis-reports/{report.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == report.id
    assert payload["task_id"] is None
    assert payload["version"] == 1

    other = await auth_client_factory("13400000049")
    forbidden = await other.get(f"/api/v1/analysis-reports/{report.id}")
    assert forbidden.status_code == 404

    session_response = await owner.get(f"/api/v1/sessions/{session_id}")
    assert session_response.status_code == 200
    summary = session_response.json()["latest_analysis_report"]
    assert summary is not None
    assert summary["id"] == report.id
    assert summary["task_id"] is None


@pytest.mark.asyncio
async def test_build_session_report_version_query_uses_locking_read(
    auth_client_factory, db_session
) -> None:
    """version 计算必须是锁定读（FOR UPDATE）：并发双击在 DB 层串行化而非失败。"""
    client = await auth_client_factory("13400000053")
    session_id, task_id = await _create_agent_session(client, "locking-read")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        service = AnalysisReportService(db_session)
        await service.build_session_report(
            user_id=source.user_id, session_id=session_id, document=_document()
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    version_queries = [
        statement
        for statement in statements
        if "max(" in statement.lower() and "analysis_reports" in statement
    ]
    assert version_queries, "未捕获到 version 计算查询"
    assert all("for update" in statement.lower() for statement in version_queries)


def _patch_flaky_flush(monkeypatch, *, failures: int) -> list[int]:
    """把 AsyncSession.flush 包成前 failures 次带报告 flush 抛 IntegrityError。

    只统计 session.new 里带 AnalysisReport 的 flush（version 查询的 autoflush
    不计入），返回计数列表供断言。
    """
    real_flush = AsyncSession.flush
    report_flushes: list[int] = []

    async def flaky_flush(self: AsyncSession) -> None:
        if any(isinstance(obj, AnalysisReport) for obj in self.new):
            report_flushes.append(1)
            if len(report_flushes) <= failures:
                raise IntegrityError("INSERT INTO analysis_reports", {}, Exception("duplicate"))
        await real_flush(self)

    monkeypatch.setattr(AsyncSession, "flush", flaky_flush)
    return report_flushes


@pytest.mark.asyncio
async def test_build_session_report_retries_after_version_conflict(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """首次 flush 撞 (session_id, version) 约束后，重算 version 重试成功。"""
    client = await auth_client_factory("13400000050")
    session_id, task_id = await _create_agent_session(client, "retry")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    report_flushes = _patch_flaky_flush(monkeypatch, failures=1)

    service = AnalysisReportService(db_session)
    report = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    assert len(report_flushes) == 2
    assert report.version == 1
    assert report.task_id is None
    persisted = await db_session.scalar(
        select(func.count(AnalysisReport.id)).where(AnalysisReport.session_id == session_id)
    )
    assert persisted == 1


@pytest.mark.asyncio
async def test_build_session_report_double_conflict_raises_domain_error(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """两次写入均撞唯一约束：抛 LookupError("report_version_conflict")（端点映射 409）。"""
    client = await auth_client_factory("13400000051")
    session_id, task_id = await _create_agent_session(client, "double-conflict")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    report_flushes = _patch_flaky_flush(monkeypatch, failures=2)

    service = AnalysisReportService(db_session)
    with pytest.raises(LookupError, match="report_version_conflict"):
        await service.build_session_report(
            user_id=source.user_id, session_id=session_id, document=_document()
        )
    assert len(report_flushes) == 2


@pytest.mark.asyncio
async def test_session_report_rejects_soft_deleted_session(
    auth_client_factory, db_session
) -> None:
    """软删会话：build_session_report 抛 session_not_found，既有报告读取 404。"""
    owner = await auth_client_factory("13400000052")
    session_id, task_id = await _create_agent_session(owner, "soft-delete")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    report = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    session = await db_session.get(WorkspaceSession, session_id)
    assert session is not None
    session.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await db_session.flush()

    with pytest.raises(LookupError, match="session_not_found"):
        await service.build_session_report(
            user_id=source.user_id, session_id=session_id, document=_document()
        )

    response = await owner.get(f"/api/v1/analysis-reports/{report.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_build_session_report_versions_scoped_by_report_type(
    auth_client_factory, db_session
) -> None:
    """version 按 (session_id, report_type) 独立编号：不同类型互不占号。"""
    client = await auth_client_factory("13400000054")
    session_id, task_id = await _create_agent_session(client, "report-type")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    kol_v1 = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    kol_v2 = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    brand_v1 = await service.build_session_report(
        user_id=source.user_id,
        session_id=session_id,
        document=_document(),
        report_type="brand_analysis",
    )
    kol_v3 = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    assert (kol_v1.version, kol_v2.version, kol_v3.version) == (1, 2, 3)
    assert brand_v1.version == 1
    assert kol_v1.report_type == "kol_analysis"
    assert brand_v1.report_type == "brand_analysis"
    persisted = await db_session.scalar(
        select(func.count(AnalysisReport.id)).where(AnalysisReport.session_id == session_id)
    )
    assert persisted == 4


@pytest.mark.asyncio
async def test_build_session_report_persists_scope_json(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000055")
    session_id, task_id = await _create_agent_session(client, "scope")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    scoped = await service.build_session_report(
        user_id=source.user_id,
        session_id=session_id,
        document=_document(),
        scope={"brand": "海底捞", "category": "美食"},
    )
    assert scoped.scope_json == {"brand": "海底捞", "category": "美食"}

    plain = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    assert plain.scope_json is None


@pytest.mark.asyncio
async def test_latest_session_report_filters_by_report_type(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000056")
    session_id, task_id = await _create_agent_session(client, "latest-by-type")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    brand = await service.build_session_report(
        user_id=source.user_id,
        session_id=session_id,
        document=_document(),
        report_type="brand_analysis",
    )
    kol_v2 = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    latest = await service.latest_session_report(session_id)
    assert latest is not None
    assert latest.id == kol_v2.id
    latest_brand = await service.latest_session_report(
        session_id, report_type="brand_analysis"
    )
    assert latest_brand is not None
    assert latest_brand.id == brand.id

