"""Task 19 API 测试：/api/v1/agent 的 Artifact 读取 API（设计 §15.2）。

覆盖：
1. 列表（module / parent_artifact_id 过滤）；
2. 详情 + 版本；
3. 未读水位：monotonic max 且后端校验不超过当前 Session sequence；
4. 导出：支持类型返回 xlsx bytes，不支持 409 ARTIFACT_EXPORT_UNSUPPORTED；
5. 全部归属失败 → 404（无存在泄露）。
"""

from __future__ import annotations

import json
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactEvent,
)
from app.agent_runtime.models import AgentRun
from app.agent_runtime.repository import utc_now
from tests.agent_artifacts.test_payloads import build_brand_dict


def _json_safe(payload: dict) -> dict:
    """把含 date/datetime 的 payload 转成 JSON 可落库形式（ISO 字符串）。"""
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


async def _me_id(client) -> str:
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _create_session(client) -> str:
    resp = await client.post("/api/v1/agent/sessions", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _insert_legacy_session(db: AsyncSession, user_id: str, session_id: str) -> None:
    """artifact_read_states.session_id 的 FK 目标仍是遗留 sessions.id（设计 §8.1）。"""
    now = utc_now()
    await db.execute(
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


async def _make_published_artifact(
    db: AsyncSession,
    user_id: str,
    session_id: str,
    *,
    module: str = "brand",
    artifact_type: str = "brand_report_v3",
    artifact_key: str | None = None,
    payload: dict | None = None,
    parent_artifact_id: str | None = None,
    latest_version: int = 1,
    data_status: str = "complete",
) -> AgentArtifact:
    """创建 user/session 下的一条已发布 Artifact（含 run + draft + revision + version）。"""
    if payload is not None:
        payload = _json_safe(payload)
    now = utc_now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="completed",
        decision_count=0,
        review_count=0,
        revision_count=0,
        completed_at=now,
    )
    db.add(run)
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        module=module,
        artifact_type=artifact_type,
        parent_artifact_id=parent_artifact_id,
        artifact_key=artifact_key or f"{module}:{uuid4().hex[:8]}",
        status="published",
        latest_version=latest_version,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db.add(artifact)
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session_id,
        owner_run_id=None,
        current_revision=1,
        status="idle",
        review_count=0,
        revision_count=1,
        updated_at=now,
    )
    db.add(draft)
    # 依赖序 flush：AgentRun 自引用 parent_run_id 且 revision↔version 存在循环 FK
    # （parent_artifact_version_id use_alter），unit-of-work 不保证插入顺序，必须
    # 逐层落库保证 FK 目标存在。
    await db.flush()  # run / artifact / draft
    revision = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run.id,
        revision=1,
        schema_version=artifact_type,
        payload_json=payload,
        evidence_refs_json=[],
        parent_artifact_version_id=None,
        payload_hash="h",
        created_at=now,
    )
    db.add(revision)
    await db.flush()  # revision 先落库，version.source_draft_revision_id 才有目标
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=revision.id,
        parent_artifact_version_id=None,
        schema_version=artifact_type,
        payload_json=payload,
        evidence_refs_json=[],
        review_json=None,
        data_status=data_status,
        created_at=now,
    )
    db.add(version)
    await db.flush()
    return artifact


async def _add_artifact_event(
    db: AsyncSession, user_id: str, session_id: str, artifact_id: str, sequence: int
) -> None:
    db.add(
        ArtifactEvent(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            sequence=sequence,
            module="brand",
            artifact_id=artifact_id,
            event_type="published",
            draft_revision=None,
            artifact_version_id=None,
            created_at=utc_now(),
        )
    )
    await db.flush()


# ---------------------------------------------------------------------------
# 列表 / 详情 / 版本
# ---------------------------------------------------------------------------


async def test_list_artifacts_filters_by_module_and_parent(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13700000001")
    session_id = await _create_session(client)
    user_id = await _me_id(client)

    brand = await _make_published_artifact(
        db_session, user_id, session_id, module="brand", artifact_type="brand_report_v3"
    )
    campaign = await _make_published_artifact(
        db_session,
        user_id,
        session_id,
        module="campaign",
        artifact_type="campaign_report_v2",
        artifact_key="campaign:brand:x",
    )
    insight = await _make_published_artifact(
        db_session,
        user_id,
        session_id,
        module="brand",
        artifact_type="insight_board_v1",
        artifact_key=f"insight:{uuid4().hex[:8]}",
        parent_artifact_id=brand.id,
    )

    listed = await client.get(f"/api/v1/agent/sessions/{session_id}/artifacts")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {brand.id, campaign.id, insight.id}

    brand_only = await client.get(
        f"/api/v1/agent/sessions/{session_id}/artifacts", params={"module": "brand"}
    )
    assert {item["id"] for item in brand_only.json()} == {brand.id, insight.id}

    children = await client.get(
        f"/api/v1/agent/sessions/{session_id}/artifacts",
        params={"parent_artifact_id": brand.id},
    )
    assert [item["id"] for item in children.json()] == [insight.id]


async def test_get_artifact_and_version(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13700000002")
    session_id = await _create_session(client)
    user_id = await _me_id(client)
    artifact = await _make_published_artifact(db_session, user_id, session_id)

    detail = await client.get(f"/api/v1/agent/artifacts/{artifact.id}")
    assert detail.status_code == 200
    assert detail.json()["artifact_type"] == "brand_report_v3"
    assert detail.json()["status"] == "published"

    version = await client.get(f"/api/v1/agent/artifacts/{artifact.id}/versions/1")
    assert version.status_code == 200
    assert version.json()["schema_version"] == "brand_report_v3"
    assert version.json()["data_status"] == "complete"

    missing = await client.get(f"/api/v1/agent/artifacts/{artifact.id}/versions/99")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 未读水位
# ---------------------------------------------------------------------------


async def test_read_state_monotonic_max_and_sequence_validation(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13700000003")
    session_id = await _create_session(client)
    user_id = await _me_id(client)
    await _insert_legacy_session(db_session, user_id, session_id)
    artifact = await _make_published_artifact(db_session, user_id, session_id)
    # Session 级 artifact sequence 当前为 3
    await _add_artifact_event(db_session, user_id, session_id, artifact.id, 1)
    await _add_artifact_event(db_session, user_id, session_id, artifact.id, 2)
    await _add_artifact_event(db_session, user_id, session_id, artifact.id, 3)

    first = await client.put(
        f"/api/v1/agent/sessions/{session_id}/artifact-read-state",
        json={"module": "brand", "last_seen_sequence": 2},
    )
    assert first.status_code == 200
    assert first.json()["last_seen_sequence"] == 2

    # 更小的 stale 写入不后退（monotonic max）
    stale = await client.put(
        f"/api/v1/agent/sessions/{session_id}/artifact-read-state",
        json={"module": "brand", "last_seen_sequence": 1},
    )
    assert stale.status_code == 200
    assert stale.json()["last_seen_sequence"] == 2

    # 超过当前 Session sequence → 422
    overshoot = await client.put(
        f"/api/v1/agent/sessions/{session_id}/artifact-read-state",
        json={"module": "brand", "last_seen_sequence": 5},
    )
    assert overshoot.status_code == 422


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


async def test_export_supported_type_returns_xlsx(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13700000004")
    session_id = await _create_session(client)
    user_id = await _me_id(client)
    artifact = await _make_published_artifact(
        db_session, user_id, session_id, payload=build_brand_dict()
    )

    resp = await client.get(f"/api/v1/agent/artifacts/{artifact.id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename" in resp.headers["content-disposition"]
    assert resp.content[:2] == b"PK"
    wb = load_workbook(BytesIO(resp.content))
    assert "综合概览" in wb.sheetnames


async def test_export_unsupported_type_conflicts(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13700000005")
    session_id = await _create_session(client)
    user_id = await _me_id(client)

    unsupported = await _make_published_artifact(
        db_session,
        user_id,
        session_id,
        module="kol",
        artifact_type="kol_detail_v2",
        artifact_key="kol-detail:x:k",
    )
    resp = await client.get(f"/api/v1/agent/artifacts/{unsupported.id}/export")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "ARTIFACT_EXPORT_UNSUPPORTED"


async def test_export_draft_without_version_conflicts(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13700000006")
    session_id = await _create_session(client)
    user_id = await _me_id(client)
    now = utc_now()
    draft_only = AgentArtifact(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        module="brand",
        artifact_type="brand_report_v3",
        parent_artifact_id=None,
        artifact_key=f"brand:{uuid4().hex[:8]}",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(draft_only)
    await db_session.flush()

    resp = await client.get(f"/api/v1/agent/artifacts/{draft_only.id}/export")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "ARTIFACT_EXPORT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# 归属隔离（无存在泄露 → 404）
# ---------------------------------------------------------------------------


async def test_artifact_ownership_isolation_returns_404(
    auth_client_factory, db_session
) -> None:
    alice = await auth_client_factory("13700000007")
    bob = await auth_client_factory("13700000008")
    session_id = await _create_session(alice)
    alice_id = await _me_id(alice)
    bob_id = await _me_id(bob)
    artifact = await _make_published_artifact(
        db_session, alice_id, session_id, payload=build_brand_dict()
    )

    assert (
        await bob.get(f"/api/v1/agent/sessions/{session_id}/artifacts")
    ).status_code == 404
    assert (await bob.get(f"/api/v1/agent/artifacts/{artifact.id}")).status_code == 404
    assert (
        await bob.get(f"/api/v1/agent/artifacts/{artifact.id}/versions/1")
    ).status_code == 404
    assert (
        await bob.get(f"/api/v1/agent/artifacts/{artifact.id}/export")
    ).status_code == 404

    # read-state 归属失败 → 404（先为 Bob 准备会话 + 遗留行）
    await _insert_legacy_session(db_session, bob_id, session_id)
    assert (
        await bob.put(
            f"/api/v1/agent/sessions/{session_id}/artifact-read-state",
            json={"module": "brand", "last_seen_sequence": 1},
        )
    ).status_code == 404
