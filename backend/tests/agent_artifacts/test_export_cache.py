"""导出缓存服务测试（Gate C Task 6）。

同一 Version+模板只构建一次；并发不重复渲染；失败可重试；原子写入；
导出失败不调用模型/MCP；storage_key 不暴露。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.export_cache import ExportedFile, ExportCacheService, sanitize_filename
from app.agent_artifacts.exporters import ArtifactExportUnsupported
from app.agent_artifacts.models import ArtifactExport
from app.agent_artifacts.router import ExportCacheService as _  # noqa: F401

from tests.agent_artifacts.test_payloads import build_brand_dict, build_kol_selection_dict


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _CountingRenderer:
    def __init__(self, payload: dict, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.call_count = 0

    def __call__(self, payload) -> bytes:
        self.call_count += 1
        if self.fail:
            raise ArtifactExportUnsupported("brand_report_v3", reason="boom")
        return b"PK-export-content"


async def _make_version(db_session, payload: dict, *, user_id: str = "u-1") -> str:
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_artifacts.models import (
        AgentArtifact,
        AgentArtifactVersion,
        ArtifactDraft,
        ArtifactDraftRevision,
    )
    from app.agent_runtime.models import AgentRun, AgentSession

    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="缓存测试", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user_id,
        run_kind="user", visibility="user", profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", status="running", started_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    artifact = AgentArtifact(
        id=str(uuid4()), session_id=session.id, user_id=user_id, module="brand",
        artifact_type="brand_report_v3", parent_artifact_id=None,
        artifact_key="brand/x-1", status="published", latest_version=1,
        activity_sequence=0, created_at=now, updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()), artifact_id=artifact.id, session_id=session.id,
        owner_run_id=run.id, current_revision=1, status="idle",
        review_count=0, revision_count=1, updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()
    revision = ArtifactDraftRevision(
        id=str(uuid4()), draft_id=draft.id, artifact_id=artifact.id,
        run_id=run.id, revision=1, schema_version="brand_report_v3",
        payload_json=json.loads(json.dumps(payload, default=str)),
        payload_hash="h" * 64, created_at=now,
    )
    db_session.add(revision)
    await db_session.flush()
    version = AgentArtifactVersion(
        id=str(uuid4()), artifact_id=artifact.id, version=1,
        source_run_id=run.id, source_draft_revision_id=revision.id,
        schema_version="brand_report_v3",
        payload_json=json.loads(json.dumps(payload, default=str)),
        data_status="complete", created_at=now,
    )
    db_session.add(version)
    await db_session.flush()
    return version.id


async def test_export_built_once_per_version_and_template(db_session, tmp_path, user_factory) -> None:
    user = await user_factory()
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    renderer = _CountingRenderer(payload)
    service = ExportCacheService(
        db_session, storage_dir=str(tmp_path), renderer=renderer
    )
    first = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="brand_report_v1.xlsx",
    )
    second = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="brand_report_v1.xlsx",
    )
    assert renderer.call_count == 1
    assert first.sha256 == second.sha256
    assert first.content == b"PK-export-content"
    assert isinstance(first, ExportedFile)
    # 缓存行 ready 且不暴露 storage_key（ExportedFile 无该字段）。
    row = await db_session.scalar(
        select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
    )
    assert row is not None
    assert row.status == "ready"
    assert not hasattr(first, "storage_key")


async def test_second_get_or_build_reuses_ready_row(db_session, tmp_path, user_factory) -> None:
    """同一 Version+模板二次构建只渲染一次（ready 行复用，行锁+唯一约束串行化）。"""
    user = await user_factory()
    payload = build_kol_selection_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    renderer = _CountingRenderer(payload)
    service_a = ExportCacheService(
        db_session, storage_dir=str(tmp_path), renderer=renderer
    )
    service_b = ExportCacheService(
        db_session, storage_dir=str(tmp_path), renderer=renderer
    )
    first = await service_a.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="a.xlsx",
    )
    # 第二个服务实例（独立实例、同一缓存目录）直接复用 ready 行。
    second = await service_b.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="a.xlsx",
    )
    assert first.sha256 == second.sha256
    assert renderer.call_count == 1


async def test_export_failure_marks_failed_and_retries(db_session, tmp_path, user_factory) -> None:
    user = await user_factory()
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    renderer = _CountingRenderer(payload, fail=True)
    service = ExportCacheService(
        db_session, storage_dir=str(tmp_path), renderer=renderer
    )
    with pytest.raises(ArtifactExportUnsupported):
        await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="x.xlsx",
        )
    row = await db_session.scalar(
        select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
    )
    assert row.status == "failed"
    assert row.error_code == "ARTIFACT_EXPORT_UNSUPPORTED"
    # 重试：换一个不失败的渲染器 → building → ready。
    renderer.ok = True
    renderer.fail = False
    result = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="x.xlsx",
    )
    assert result.content == b"PK-export-content"


async def test_export_failure_does_not_call_model_or_mcp(db_session, tmp_path, monkeypatch, user_factory) -> None:
    from app.agent_artifacts.exporters import export_artifact as real_export

    user = await user_factory()
    calls = {"model": 0, "mcp": 0}

    def _guarded_export(version, *, model=None, gateway=None):
        if model is not None:
            calls["model"] += 1
        if gateway is not None:
            calls["mcp"] += 1
        return real_export(version)

    monkeypatch.setattr("app.agent_artifacts.export_cache.export_artifact", _guarded_export)
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path))
    result = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="x.xlsx",
    )
    assert result.content[:2] == b"PK"
    assert calls["model"] == 0
    assert calls["mcp"] == 0


def test_sanitize_filename_strips_unsafe_chars() -> None:
    assert sanitize_filename("a/b\\c:d?.xlsx") == "a_b_c_d_.xlsx"
    assert sanitize_filename("brand_report_v1.xlsx") == "brand_report_v1.xlsx"
    assert sanitize_filename("...") == "artifact"


# ---------------------------------------------------------------------------
# Gate C 审核修复 A5：真实并发 / 取消接管 / stale 恢复 / 文件校验
# ---------------------------------------------------------------------------


async def _make_version_committed(payload: dict) -> tuple[str, str]:
    """用真实 SessionFactory 提交完整 version 链；返回 (version.id, user.id)。"""
    import json as _json
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_artifacts.models import (
        AgentArtifact,
        AgentArtifactVersion,
        ArtifactDraft,
        ArtifactDraftRevision,
    )
    from app.agent_runtime.models import AgentRun, AgentSession
    from app.identity.models import User
    from app.db.session import SessionFactory

    now = datetime.now(UTC).replace(tzinfo=None)
    payload_json = _json.loads(_json.dumps(payload, default=str))
    async with SessionFactory.begin() as db:
        user = User(id=str(uuid4()), nickname="cache-conc", role="user", status="active",
                    created_at=now, updated_at=now)
        db.add(user)
        await db.flush()
        session = AgentSession(id=str(uuid4()), user_id=user.id, title="缓存并发",
                               status="active", created_at=now, updated_at=now)
        db.add(session)
        await db.flush()
        run = AgentRun(id=str(uuid4()), session_id=session.id, user_id=user.id,
                       run_kind="user", visibility="user", profile_name="session_analyst_v1",
                       profile_version="v1", model="t", status="running", started_at=now)
        db.add(run)
        await db.flush()
        artifact = AgentArtifact(
            id=str(uuid4()), session_id=session.id, user_id=user.id, module="brand",
            artifact_type="brand_report_v3", parent_artifact_id=None,
            artifact_key="brand/x-c", status="published", latest_version=1,
            activity_sequence=0, created_at=now, updated_at=now,
        )
        db.add(artifact)
        await db.flush()
        draft = ArtifactDraft(
            id=str(uuid4()), artifact_id=artifact.id, session_id=session.id,
            owner_run_id=run.id, current_revision=1, status="idle",
            review_count=0, revision_count=1, updated_at=now,
        )
        db.add(draft)
        await db.flush()
        revision = ArtifactDraftRevision(
            id=str(uuid4()), draft_id=draft.id, artifact_id=artifact.id,
            run_id=run.id, revision=1, schema_version="brand_report_v3",
            payload_json=payload_json, payload_hash="h" * 64, created_at=now,
        )
        db.add(revision)
        await db.flush()
        version = AgentArtifactVersion(
            id=str(uuid4()), artifact_id=artifact.id, version=1,
            source_run_id=run.id, source_draft_revision_id=revision.id,
            schema_version="brand_report_v3", payload_json=payload_json,
            data_status="complete", created_at=now,
        )
        db.add(version)
        await db.flush()
        return version.id, user.id


async def test_real_concurrent_sessions_render_once(tmp_path) -> None:
    """两个独立 SessionFactory 连接并发：只渲染一次。"""
    from app.db.session import SessionFactory


    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    renderer = _CountingRenderer(payload)

    async def _build():
        async with SessionFactory() as db:
            service = ExportCacheService(
                db, storage_dir=str(tmp_path), renderer=renderer
            )
            return await service.get_or_build(
                artifact_version_id=version_id, schema_version="brand_report_v3",
                payload=payload, filename="a.xlsx",
            )

    results = await asyncio.gather(_build(), _build())
    assert results[0].sha256 == results[1].sha256
    assert renderer.call_count == 1
    await _purge_committed(user_id)


async def test_cancelled_owner_then_takeover(tmp_path) -> None:
    """owner 任务取消后，后续请求能接管并成功。"""
    from app.db.session import SessionFactory


    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)

    def _hanging_renderer(payload):
        import time

        time.sleep(10)  # 阻塞线程挂起（to_thread 等待点可被取消）
        return b"never"

    async def _owner():
        async with SessionFactory() as db:
            service = ExportCacheService(
                db, storage_dir=str(tmp_path), renderer=_hanging_renderer,
                lease_seconds=60,
            )
            await service.get_or_build(
                artifact_version_id=version_id, schema_version="brand_report_v3",
                payload=payload, filename="a.xlsx",
            )

    owner_task = asyncio.create_task(_owner())
    await asyncio.sleep(0.4)  # owner 进入渲染（building 行已提交）
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    # 后续请求接管（cancelled → failed 或 stale building 均可恢复）。
    renderer = _CountingRenderer(payload)
    async with SessionFactory() as db:
        service = ExportCacheService(
            db, storage_dir=str(tmp_path), renderer=renderer, lease_seconds=0.01
        )
        result = await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
    assert result.content == b"PK-export-content"
    await _purge_committed(user_id)


async def test_stale_building_row_is_taken_over(tmp_path) -> None:
    """预置过期 building 行，后续请求能够恢复。"""
    from datetime import timedelta

    from app.db.session import SessionFactory


    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    # 预置 600 秒前的 building 行（超出租约）。
    from app.agent_artifacts.export_cache import _now as cache_now

    async with SessionFactory.begin() as db:
        db.add(ArtifactExport(
            id=str(uuid4()), artifact_version_id=version_id,
            template_version="brand_report_v3", status="building",
            created_at=cache_now() - timedelta(seconds=600),
        ))
    renderer = _CountingRenderer(payload)
    async with SessionFactory() as db:
        service = ExportCacheService(
            db, storage_dir=str(tmp_path), renderer=renderer, lease_seconds=60
        )
        result = await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
    assert result.content == b"PK-export-content"
    assert renderer.call_count == 1
    await _purge_committed(user_id)


async def test_ready_file_deleted_rebuilds(tmp_path) -> None:
    """ready 文件被删除后可重新生成。"""
    from app.db.session import SessionFactory


    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    renderer = _CountingRenderer(payload)
    async with SessionFactory() as db:
        service = ExportCacheService(db, storage_dir=str(tmp_path), renderer=renderer)
        await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
    # 删除缓存文件（模拟存储丢失）。
    for path in tmp_path.glob("*.xlsx"):
        path.unlink()
    async with SessionFactory() as db:
        service = ExportCacheService(db, storage_dir=str(tmp_path), renderer=renderer)
        second = await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
    assert second.content == b"PK-export-content"
    assert renderer.call_count == 2  # 文件丢失 → 重建
    await _purge_committed(user_id)


async def test_cache_hit_does_not_call_renderer(tmp_path) -> None:
    """缓存命中不调用 renderer。"""
    from app.db.session import SessionFactory


    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    renderer = _CountingRenderer(payload)
    async with SessionFactory() as db:
        service = ExportCacheService(db, storage_dir=str(tmp_path), renderer=renderer)
        await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
        assert renderer.call_count == 1
        await service.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
        assert renderer.call_count == 1  # 命中不渲染
    await _purge_committed(user_id)


# ---------------------------------------------------------------------------
# Gate C 复审：claim_token owner fencing（条件更新完成/接管）
# ---------------------------------------------------------------------------


async def test_mark_ready_fenced_by_claim_token_mismatch(
    db_session, tmp_path, user_factory
) -> None:
    """条件更新 owner fencing：claim_token 不匹配的完成更新不得生效。

    僵尸构建方（租约超时被接管后才回来提交）持有的旧 claim_token 与行上当前
    owner 不符 → mark_ready 影响 0 行，绝不覆盖新 owner 的状态。
    """
    user = await user_factory()
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    db_session.add(
        ArtifactExport(
            id=str(uuid4()),
            artifact_version_id=version_id,
            template_version="brand_report_v3",
            status="building",
            claim_token="owner-B",
            created_at=_now(),
        )
    )
    await db_session.commit()
    service = ExportCacheService(db_session, storage_dir=str(tmp_path))

    fenced = await service._mark_ready(
        version_id,
        "brand_report_v3",
        filename="zombie.xlsx",
        storage_key="zombie.xlsx",
        content=b"zombie",
        claim_token="owner-A",
    )
    assert fenced is False
    row = await db_session.scalar(
        select(ArtifactExport)
        .where(ArtifactExport.artifact_version_id == version_id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "building"
    assert row.claim_token == "owner-B"

    owner_ok = await service._mark_ready(
        version_id,
        "brand_report_v3",
        filename="owner.xlsx",
        storage_key="owner.xlsx",
        content=b"owner-content",
        claim_token="owner-B",
    )
    assert owner_ok is True
    row = await db_session.scalar(
        select(ArtifactExport)
        .where(ArtifactExport.artifact_version_id == version_id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "ready"
    assert row.filename == "owner.xlsx"


async def test_stale_takeover_reassigns_claim_token(tmp_path) -> None:
    """接管 stale building 行必须换新 claim_token，旧 owner 被 fence 出局。"""
    from datetime import timedelta

    from app.agent_artifacts.export_cache import _now as cache_now
    from app.db.session import SessionFactory

    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    async with SessionFactory.begin() as db:
        db.add(
            ArtifactExport(
                id=str(uuid4()),
                artifact_version_id=version_id,
                template_version="brand_report_v3",
                status="building",
                claim_token="stale-old-owner",
                created_at=cache_now() - timedelta(seconds=600),
            )
        )
    renderer = _CountingRenderer(payload)
    async with SessionFactory() as db:
        service = ExportCacheService(
            db, storage_dir=str(tmp_path), renderer=renderer, lease_seconds=60
        )
        result = await service.get_or_build(
            artifact_version_id=version_id,
            schema_version="brand_report_v3",
            payload=payload,
            filename="a.xlsx",
        )
    assert result.content == b"PK-export-content"
    async with SessionFactory() as db:
        row = await db.scalar(
            select(ArtifactExport).where(
                ArtifactExport.artifact_version_id == version_id
            )
        )
        assert row.status == "ready"
        assert row.claim_token is not None
        assert row.claim_token != "stale-old-owner"
    await _purge_committed(user_id)


async def _purge_committed(user_id: str) -> None:
    """按 FK 顺序清理真实提交的测试链（export_cache 并发测试专用）。"""
    from app.agent_artifacts.models import (
        AgentArtifact,
        AgentArtifactVersion,
        ArtifactDraft,
        ArtifactDraftRevision,
    )
    from app.agent_runtime.models import AgentRun, AgentSession
    from app.identity.models import User
    from app.db.session import SessionFactory

    async with SessionFactory() as db:
        artifact_ids = list(
            (
                await db.scalars(
                    select(AgentArtifact.id).where(AgentArtifact.user_id == user_id)
                )
            ).all()
        )
        version_ids = list(
            (
                await db.scalars(
                    select(AgentArtifactVersion.id).where(
                        AgentArtifactVersion.artifact_id.in_(artifact_ids)
                    )
                )
            ).all()
        )
        if version_ids:
            for row in (
                await db.scalars(
                    select(ArtifactExport).where(
                        ArtifactExport.artifact_version_id.in_(version_ids)
                    )
                )
            ).all():
                await db.delete(row)
            for row in (
                await db.scalars(
                    select(AgentArtifactVersion).where(
                        AgentArtifactVersion.artifact_id.in_(artifact_ids)
                    )
                )
            ).all():
                await db.delete(row)
        for row in (
            await db.scalars(
                select(ArtifactDraftRevision).where(
                    ArtifactDraftRevision.artifact_id.in_(artifact_ids)
                )
            )
        ).all():
            await db.delete(row)
        for row in (
            await db.scalars(
                select(ArtifactDraft).where(ArtifactDraft.artifact_id.in_(artifact_ids))
            )
        ).all():
            await db.delete(row)
        for row in (
            await db.scalars(
                select(AgentArtifact).where(AgentArtifact.user_id == user_id)
            )
        ).all():
            await db.delete(row)
        for row in (
            await db.scalars(select(AgentRun).where(AgentRun.user_id == user_id))
        ).all():
            await db.delete(row)
        for row in (
            await db.scalars(select(AgentSession).where(AgentSession.user_id == user_id))
        ).all():
            await db.delete(row)
        user = await db.get(User, user_id)
        if user is not None:
            await db.delete(user)
        await db.commit()
