"""不可变 Evidence 测试（设计文档 §8.1 / §10.2）。

Evidence 写入后不可更新、只能追加；payload_hash 由 raw_payload_json 的
canonical JSON 计算；模型只得到 evidence_id + 有限预览，绝看不到完整原始
payload。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.evidence import EvidenceWriter, build_preview, model_view
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.normalization import NormalizationRegistry
from app.mcp_gateway.validation import canonical_json_bytes

FULL_PAYLOAD = {
    "result": json.dumps(
        {"rows": [{"keyword": "美妆", "volume": 123456} for _ in range(50)], "total": 50},
        ensure_ascii=False,
    )
}
LONG_PAYLOAD = {"result": "数据" * 3000}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _make_chain(db_session, user_id: str) -> tuple[AgentSession, AgentRun, AgentStep, AgentToolCall]:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="会话", status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id="ev-lc-1",
        service="insight-cube-mcp",
        internal_tool_name="query_analysis_data",
        arguments_json={"keyword": "美妆"},
        arguments_hash="e" * 64,
        status="settled",
        points_settled=10,
    )
    db_session.add(call)
    await db_session.flush()
    return session, run, step, call


async def _write(
    db_session, user_factory, *, tool_call_id: str | None = None, raw_payload=FULL_PAYLOAD
) -> tuple[EvidenceItem, AgentSession, AgentRun]:
    user = await user_factory()
    session, run, step, call = await _make_chain(db_session, user.id)
    writer = EvidenceWriter(db_session)
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=tool_call_id or call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json={"brand": "示例品牌"},
        period_json=None,
        raw_payload=raw_payload,
        collected_at=_now(),
        availability_status="available",
    )
    return item, session, run


@pytest.mark.asyncio
async def test_evidence_inserts_row_with_hash_and_preview(db_session, user_factory) -> None:
    item, session, run = await _write(db_session, user_factory)

    row = await db_session.get(EvidenceItem, item.id)
    assert row is not None
    assert row.session_id == session.id
    assert row.run_id == run.id
    assert row.source_type == "mcp"
    assert row.source_name == "query_analysis_data"
    assert row.scope_json == {"brand": "示例品牌"}
    assert row.period_json is None
    # 完整原始结果落库
    assert row.raw_payload_json == FULL_PAYLOAD
    # payload_hash 由 raw_payload_json 计算
    assert row.payload_hash == hashlib.sha256(canonical_json_bytes(FULL_PAYLOAD)).hexdigest()
    # 模型可见的预览是受限的，不是完整 payload
    assert row.normalized_preview_json is not None
    assert row.normalized_preview_json != FULL_PAYLOAD
    assert row.availability_status == "available"
    assert row.collected_at is not None


@pytest.mark.asyncio
async def test_evidence_write_persists_normalization_diagnostics(
    db_session, user_factory
) -> None:
    """EvidenceWriter 同一事务保存归一化诊断（Gate B：字段映射随 Evidence 落库）。"""
    user = await user_factory()
    session, run, step, call = await _make_chain(db_session, user.id)
    writer = EvidenceWriter(db_session)
    normalization = NormalizationRegistry().normalize(
        "query_analysis_data",
        {"data": [{"日": "2026-08-01", "声量": 12}]},
    )
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload={"data": [{"日": "2026-08-01", "声量": 12}]},
        collected_at=_now(),
        normalization=normalization,
    )
    assert item.normalization_version == normalization.version
    assert item.normalization_status == "normalized"
    assert item.field_mapping_json == normalization.field_mapping
    assert item.unmapped_fields_json == list(normalization.unmapped_fields)
    assert item.normalization_error_code is None


@pytest.mark.asyncio
async def test_evidence_write_without_normalization_keeps_diagnostic_columns_null(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session, run, step, call = await _make_chain(db_session, user.id)
    writer = EvidenceWriter(db_session)
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload=FULL_PAYLOAD,
        collected_at=_now(),
    )
    assert item.normalization_version is None
    assert item.normalization_status is None
    assert item.normalization_error_code is None


@pytest.mark.asyncio
async def test_evidence_is_append_only_and_has_no_update_path(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, step, call = await _make_chain(db_session, user.id)
    writer = EvidenceWriter(db_session)
    # 同 key（session/run/tool_call）写两次 → 两行（append-only，不是 upsert）
    for _ in range(2):
        await writer.write(
            session_id=session.id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="mcp",
            source_name="query_analysis_data",
            scope_json=None,
            period_json=None,
            raw_payload=FULL_PAYLOAD,
            collected_at=_now(),
        )

    rows = (
        await db_session.scalars(
            select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
        )
    ).all()
    assert len(rows) == 2
    # writer 不暴露任何 update/mutate 路径
    assert not hasattr(writer, "update")
    assert not hasattr(writer, "save")


@pytest.mark.asyncio
async def test_evidence_preview_is_limited_not_full_payload(db_session, user_factory) -> None:
    item, _session, _run = await _write(db_session, user_factory, raw_payload=LONG_PAYLOAD)

    preview = item.normalized_preview_json
    preview_text = json.dumps(preview, ensure_ascii=False)
    full_text = json.dumps(LONG_PAYLOAD, ensure_ascii=False)
    assert len(preview_text) < len(full_text)
    # 完整长数据绝不进入模型可见的预览
    assert "数据" * 3000 not in preview_text
    assert item.raw_payload_json == LONG_PAYLOAD


@pytest.mark.asyncio
async def test_model_only_sees_evidence_id_and_preview(db_session, user_factory) -> None:
    item, _session, _run = await _write(db_session, user_factory)

    view = model_view(item)
    assert view["evidence_id"] == item.id
    # 模型可见视图只包含证据 id 与预览
    assert "evidence_id" in view and "preview" in view
    assert "raw_payload_json" not in view
    assert "payload_hash" not in view
    assert view["preview"] == item.normalized_preview_json
    assert view["preview"] != FULL_PAYLOAD


def test_build_preview_caps_row_count_and_fields() -> None:
    payload = {"result": json.dumps({"rows": [{"k": f"v{i}"} for i in range(100)]})}
    preview = build_preview(payload)
    # 预览必须带行数/截断标记/可用字段（§10.2）
    assert "row_count" in preview
    assert "truncated" in preview
    assert "available_fields" in preview
    assert "payload_hash" in preview
    assert preview["row_count"] == 100
    assert preview["truncated"] is True
