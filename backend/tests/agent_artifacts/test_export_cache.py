"""导出缓存服务测试（Gate C Task 6）。

同一 Version+模板只构建一次；并发不重复渲染；失败可重试；原子写入；
导出失败不调用模型/MCP；storage_key 不暴露。
"""

from __future__ import annotations


import pytest
from sqlalchemy import select

from app.agent_artifacts.export_cache import ExportedFile, ExportCacheService, sanitize_filename
from app.agent_artifacts.exporters import ArtifactExportUnsupported
from app.agent_artifacts.models import ArtifactExport
from app.agent_artifacts.router import ExportCacheService as _  # noqa: F401

from tests.agent_artifacts.test_payloads import build_brand_dict, build_kol_selection_dict


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
