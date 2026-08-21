"""导出缓存服务测试（Gate C Task 6）。

同一 Version+模板只构建一次；并发不重复渲染；失败可重试；原子写入；
导出失败不调用模型/MCP；storage_key 不暴露。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.export_cache import ExportedFile, ExportCacheService, sanitize_filename
from app.agent_artifacts.exporters import ArtifactExportUnsupported, workbook_layout_digest
from app.agent_artifacts.models import ArtifactExport
from app.agent_artifacts.router import ExportCacheService as _  # noqa: F401

from tests.agent_artifacts.test_analysis_report_export import build_analysis_report_payload
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


async def _make_version(
    db_session,
    payload: dict,
    *,
    user_id: str = "u-1",
    schema_version: str = "brand_report_v3",
    module: str = "brand",
    artifact_type: str | None = None,
) -> str:
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
        id=str(uuid4()), session_id=session.id, user_id=user_id, module=module,
        artifact_type=artifact_type or schema_version, parent_artifact_id=None,
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
        run_id=run.id, revision=1, schema_version=schema_version,
        payload_json=json.loads(json.dumps(payload, default=str)),
        payload_hash="h" * 64, created_at=now,
    )
    db_session.add(revision)
    await db_session.flush()
    version = AgentArtifactVersion(
        id=str(uuid4()), artifact_id=artifact.id, version=1,
        source_run_id=run.id, source_draft_revision_id=revision.id,
        schema_version=schema_version,
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


# ---------------------------------------------------------------------------
# Gate C 第三轮：导出缓存异常路径
#
# ready 命中必须同时校验 filename/storage_key/sha256/size_bytes；元数据不完整、
# 文件缺失、hash 不匹配统一失效重建且删除损坏文件；render/write_atomic/
# mark_ready 三阶段的 CancelledError 都要安全收尾；丢失租约的 owner 只能清理
# 自己的文件不能更新缓存行；OperationalError 只对 MySQL 可重试死锁/锁等待做
# 有限次数指数退避，连接断开等错误直接抛出。
# ---------------------------------------------------------------------------


async def test_ready_row_with_null_storage_key_rebuilds(db_session, tmp_path, user_factory) -> None:
    """ready 行 storage_key 为空（元数据不完整）：不得命中，失效重建。"""
    import hashlib

    user = await user_factory()
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    db_session.add(
        ArtifactExport(
            id=str(uuid4()),
            artifact_version_id=version_id,
            template_version="brand_report_v3",
            status="ready",
            filename="a.xlsx",
            storage_key=None,
            sha256=hashlib.sha256(b"stale").hexdigest(),
            size_bytes=5,
            claim_token="t",
            created_at=_now(),
        )
    )
    await db_session.commit()
    renderer = _CountingRenderer(payload)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path), renderer=renderer)
    result = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="a.xlsx",
    )
    assert result.content == b"PK-export-content"
    assert renderer.call_count == 1  # 元数据不完整 → 必须重建
    row = await db_session.scalar(
        select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "ready"
    assert row.storage_key is not None


async def test_ready_hash_mismatch_deletes_corrupt_file_and_rebuilds(
    db_session, tmp_path, user_factory,
) -> None:
    """ready 文件 hash 不匹配：删除旧损坏文件后重建。"""
    import hashlib

    user = await user_factory()
    payload = build_brand_dict()
    version_id = await _make_version(db_session, payload, user_id=user.id)
    corrupt_key = "corrupt.xlsx"
    (tmp_path / corrupt_key).write_bytes(b"corrupt-content")
    db_session.add(
        ArtifactExport(
            id=str(uuid4()),
            artifact_version_id=version_id,
            template_version="brand_report_v3",
            status="ready",
            filename="a.xlsx",
            storage_key=corrupt_key,
            sha256=hashlib.sha256(b"expected-other").hexdigest(),
            size_bytes=len(b"corrupt-content"),
            claim_token="t",
            created_at=_now(),
        )
    )
    await db_session.commit()
    renderer = _CountingRenderer(payload)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path), renderer=renderer)
    result = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="a.xlsx",
    )
    assert result.content == b"PK-export-content"
    assert renderer.call_count == 1  # 损坏 → 本次重建
    assert not (tmp_path / corrupt_key).exists()  # 旧损坏文件被删除
    second = await service.get_or_build(
        artifact_version_id=version_id, schema_version="brand_report_v3",
        payload=payload, filename="a.xlsx",
    )
    assert second.content == b"PK-export-content"
    assert renderer.call_count == 1  # 重建后命中缓存


async def test_cancel_during_write_atomic_no_late_orphan(tmp_path) -> None:
    """写入阶段取消：后台写线程晚完成不再遗留孤儿文件（Codex 并发探针复现）。

    慢写函数进入线程后阻塞 → 取消 owner task → 释放线程后慢写函数真正执行
    原子写文件（不是"阻塞结束后什么也不写"的假测试）→ 断言原 owner 的
    .xlsx/.tmp 均不存在、DB 为 failed/export_cancelled、后续请求接管成功、
    最终目录只有新 owner 的有效文件。
    """
    import threading

    from app.db.session import SessionFactory

    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _slow_then_write(path, content) -> None:
        entered.set()
        release.wait(10)
        try:
            ExportCacheService._write_atomic(path, content)
        finally:
            done.set()

    async def _owner():
        async with SessionFactory() as db:
            service = ExportCacheService(
                db, storage_dir=str(tmp_path), renderer=_CountingRenderer(payload)
            )
            service._write_atomic = _slow_then_write  # type: ignore[method-assign]
            await service.get_or_build(
                artifact_version_id=version_id, schema_version="brand_report_v3",
                payload=payload, filename="a.xlsx",
            )

    task = asyncio.create_task(_owner())
    for _attempt in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.05)
    assert entered.is_set(), "owner 未进入写入阶段"
    task.cancel()
    release.set()  # 释放后台线程，让它真正执行写文件
    with pytest.raises(asyncio.CancelledError):
        await task
    # 后台线程必须已真正结束（慢写执行完毕）。
    assert await asyncio.to_thread(done.wait, 5), "后台写线程未结束"

    # 原 owner 的 .xlsx 与 .tmp 都不存在（晚完成写入不遗留孤儿文件）。
    assert list(tmp_path.glob("*.xlsx")) == []
    assert list(tmp_path.glob("*.tmp")) == []

    async with SessionFactory() as db:
        row = await db.scalar(
            select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
        )
        assert row.status == "failed"
        assert row.error_code == "export_cancelled"

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
    # 最终目录只有新 owner 的有效文件。
    files = list(tmp_path.glob("*.xlsx"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"PK-export-content"
    await _purge_committed(user_id)


async def test_cancel_during_mark_ready_cleans_orphan_file(tmp_path) -> None:
    """mark_ready 阶段被取消：安全收尾，孤儿文件被清理，后续请求可接管。"""
    from app.db.session import SessionFactory

    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    entered = asyncio.Event()

    async def _slow_mark_ready(*args, **kwargs) -> bool:
        entered.set()
        await asyncio.sleep(10)
        return True

    async def _owner():
        async with SessionFactory() as db:
            service = ExportCacheService(
                db, storage_dir=str(tmp_path), renderer=_CountingRenderer(payload)
            )
            service._mark_ready = _slow_mark_ready  # type: ignore[method-assign]
            await service.get_or_build(
                artifact_version_id=version_id, schema_version="brand_report_v3",
                payload=payload, filename="a.xlsx",
            )

    task = asyncio.create_task(_owner())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

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
    # 孤儿文件（取消时已写但未落库）被清理，只剩新 owner 的文件。
    assert len(list(tmp_path.glob("*.xlsx"))) == 1
    await _purge_committed(user_id)


async def test_slow_owner_stale_takeover_old_owner_last_result_fenced(tmp_path) -> None:
    """慢 owner 渲染期间被 stale 接管：新 owner 完成；旧 owner 最后回来
    mark_ready 被 fence（只清理自己的文件），绝不覆盖接管方。"""
    import threading

    from app.db.session import SessionFactory

    payload = build_brand_dict()
    version_id, user_id = await _make_version_committed(payload)
    release_owner_a = threading.Event()

    def _renderer_a(payload) -> bytes:
        release_owner_a.wait(10)
        return b"owner-A-content"

    def _renderer_b(payload) -> bytes:
        return b"owner-B-content"

    async def _owner_a():
        async with SessionFactory() as db:
            service = ExportCacheService(
                db, storage_dir=str(tmp_path), renderer=_renderer_a, lease_seconds=60
            )
            await service.get_or_build(
                artifact_version_id=version_id, schema_version="brand_report_v3",
                payload=payload, filename="a.xlsx",
            )

    task_a = asyncio.create_task(_owner_a())
    await asyncio.sleep(0.4)  # A 进入渲染（阻塞）
    async with SessionFactory() as db:
        service_b = ExportCacheService(
            db, storage_dir=str(tmp_path), renderer=_renderer_b, lease_seconds=0.01
        )
        result_b = await service_b.get_or_build(
            artifact_version_id=version_id, schema_version="brand_report_v3",
            payload=payload, filename="a.xlsx",
        )
    assert result_b.content == b"owner-B-content"

    release_owner_a.set()
    await task_a  # 旧 owner 最后回来：mark_ready 被 fence，重读新 owner，不抛错

    async with SessionFactory() as db:
        row = await db.scalar(
            select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
        )
        assert row.status == "ready"
        assert (tmp_path / row.storage_key).read_bytes() == b"owner-B-content"
    assert len(list(tmp_path.glob("*.xlsx"))) == 1  # A 的孤儿文件被清理
    await _purge_committed(user_id)


class _OperationalErrorSession:
    """scalar 恒抛 OperationalError 的 stub 会话（不依赖真实 MySQL 连接，
    专测退避逻辑；rollback/commit 为 async no-op）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def scalar(self, *args, **kwargs):
        self.calls += 1
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT", {}, Exception(self.message))

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_operational_error_deadlock_bounded_retry(tmp_path) -> None:
    """MySQL 可重试死锁：有限次数指数退避后放弃，绝不无限循环。"""
    from sqlalchemy.exc import OperationalError

    from app.agent_artifacts.export_cache import _MAX_DB_RETRY_ATTEMPTS

    stub = _OperationalErrorSession(
        "Deadlock found when trying to get lock; try restarting transaction"
    )
    service = ExportCacheService(stub, storage_dir=str(tmp_path))
    with pytest.raises(OperationalError):
        await service.get_or_build(
            artifact_version_id="v-1", schema_version="brand_report_v3",
            payload=None, filename="a.xlsx",
        )
    assert stub.calls == _MAX_DB_RETRY_ATTEMPTS + 1


async def test_operational_error_connection_lost_raises_immediately(tmp_path) -> None:
    """连接断开类 OperationalError：不重试，直接抛出。"""
    from sqlalchemy.exc import OperationalError

    stub = _OperationalErrorSession("Can't connect to MySQL server on '127.0.0.1' (2003)")
    service = ExportCacheService(stub, storage_dir=str(tmp_path))
    with pytest.raises(OperationalError):
        await service.get_or_build(
            artifact_version_id="v-1", schema_version="brand_report_v3",
            payload=None, filename="a.xlsx",
        )
    assert stub.calls == 1  # 不重试


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


# --------------------------------------------------------------------------- #
# direct payload 导出回归（f15ff5d Minor #1 条件化修复验证）
# --------------------------------------------------------------------------- #


async def test_get_or_build_renders_direct_payload_with_lineage_snapshot(
    db_session, tmp_path, user_factory
) -> None:
    """lineage_snapshot={"mode": "model_direct_v1"} 时真实 export_artifact
    （_renderer=None）渲染 direct brand payload 成功——修复 f15ff5d 引入的
    direct payload 导出 409 回归（_VersionLike 缺 lineage_snapshot_json）。"""
    from app.agent_artifacts.model_inputs import assemble_model_payload
    from app.agent_artifacts.model_inputs.brand import BrandReportV3Input
    from tests.agent_artifacts.payload_fixtures import brand_model_input

    user = await user_factory()
    direct_payload = assemble_model_payload(
        "brand_report_v3",
        BrandReportV3Input.model_validate(brand_model_input()),
    )
    version_id = await _make_version(db_session, direct_payload, user_id=user.id)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path))

    result = await service.get_or_build(
        artifact_version_id=version_id,
        schema_version="brand_report_v3",
        payload=direct_payload,
        filename="brand_report_v1.xlsx",
        lineage_snapshot={"mode": "model_direct_v1"},
    )
    assert result.content.startswith(b"PK\x03\x04")
    assert result.size_bytes == len(result.content)
    row = await db_session.scalar(
        select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
    )
    assert row is not None and row.status == "ready"


async def test_get_or_build_without_lineage_snapshot_keeps_legacy_path(
    db_session, tmp_path, user_factory
) -> None:
    """不传 lineage_snapshot（legacy Evidence-backed payload / 历史 Version）：
    真实 export_artifact 走无条件路径成功，行为与 f15ff5d 之前一致。"""
    user = await user_factory()
    payload = build_brand_dict()  # canonical 带 evidence refs 的 legacy 完整 payload
    version_id = await _make_version(db_session, payload, user_id=user.id)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path))

    result = await service.get_or_build(
        artifact_version_id=version_id,
        schema_version="brand_report_v3",
        payload=payload,
        filename="brand_report_v1.xlsx",
        lineage_snapshot=None,
    )
    assert result.content.startswith(b"PK\x03\x04")


async def test_analysis_report_cache_key_includes_exporter_and_layout(
    db_session, tmp_path, user_factory
) -> None:
    user = await user_factory()
    payload = build_analysis_report_payload()
    version_id = await _make_version(
        db_session,
        payload,
        user_id=user.id,
        schema_version="analysis_report_v1",
        module="report",
    )
    from app.agent_artifacts.payloads.analysis_report import AnalysisReportV1

    report = AnalysisReportV1.model_validate(payload)
    layout_digest = workbook_layout_digest(report.workbook)
    changed_layout_digest = workbook_layout_digest(
        report.workbook.model_copy(update={"sheets": ()})
    )
    renderer = _CountingRenderer(payload)
    service = ExportCacheService(db_session, storage_dir=str(tmp_path), renderer=renderer)
    first = await service.get_or_build(
        artifact_version_id=version_id,
        schema_version="analysis_report_v1",
        payload=payload,
        filename="report.xlsx",
        exporter_version="analysis-report-v1.0.0",
        layout_digest=layout_digest,
    )
    second = await service.get_or_build(
        artifact_version_id=version_id,
        schema_version="analysis_report_v1",
        payload=payload,
        filename="report.xlsx",
        exporter_version="analysis-report-v1.0.0",
        layout_digest=layout_digest,
    )
    third = await service.get_or_build(
        artifact_version_id=version_id,
        schema_version="analysis_report_v1",
        payload=payload,
        filename="report.xlsx",
        exporter_version="analysis-report-v1.0.0",
        layout_digest=changed_layout_digest,
    )
    assert first.sha256 == second.sha256
    assert third.sha256 == first.sha256
    assert renderer.call_count == 2
    rows = (
        await db_session.scalars(
            select(ArtifactExport).where(ArtifactExport.artifact_version_id == version_id)
        )
    ).all()
    expected = {
        sha256(f"{version_id}analysis-report-v1.0.0{digest}".encode()).hexdigest()
        for digest in (layout_digest, changed_layout_digest)
    }
    assert {row.template_version for row in rows} == expected
