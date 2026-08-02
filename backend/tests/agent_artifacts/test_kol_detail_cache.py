"""kol_detail_cache 表与 TTL 测试（设计文档 §8.1 / Task 17）。

覆盖：
1. 唯一 (user_id, session_id, platform, kol_uid)：同键重复写入走 upsert，不产生重复行；
2. 存储 payload + evidence_refs + fetched_at/expires_at；
3. TTL 默认 24h 可配置（``KOL_DETAIL_CACHE_TTL_HOURS`` 通过构造参数注入）；
4. 注入时钟验证缓存过期判定；跨 Session 隔离（不同 Session 不命中）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.agent_artifacts.models import KolDetailCache
from app.agent_runtime.kol_detail import KolDetailRunService

T0 = datetime(2026, 1, 1, 12, 0, 0)

# 最小 payload：set_cached_detail 只做 cache 块规范化，不校验完整 schema。
PAYLOAD = {
    "schema_version": "kol_detail_v2",
    "module": "kol",
    "scope": {
        "platform": "xiaohongshu",
        "kol_uid": "k1",
        "selection_artifact_id": None,
        "selection_version": None,
    },
    "data_status": "complete",
    "availability": {},
    "limitations": [],
    "methodology": {
        "data_as_of": "2026-01-01T12:00:00",
        "source_names": ["kol_evidence"],
        "notes": [],
    },
    "data": {
        "identity": {"nickname": "达人K", "homepage_url": "https://example.com/k1"},
        "cache": {
            "hit": False,
            "fetched_at": "2026-01-01T12:00:00",
            "expires_at": "2026-01-02T12:00:00",
        },
    },
    "narrative": {
        "profile_summary": "概览",
        "content_strengths": [],
        "commercial_notes": [],
        "risk_notes": [],
    },
}


def _make_service(db, *, ttl: int = 24, now_fn=None) -> KolDetailRunService:
    return KolDetailRunService(
        db,
        engine=None,
        cache_ttl_hours=ttl,
        now_fn=now_fn or (lambda: T0),
        model="test-model",
    )


async def test_cache_write_read_roundtrip(db_session, user_factory, session_factory) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    service = _make_service(db_session)

    row = await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[{"artifact_path": "/data/metrics/followers", "sources": []}],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )
    assert row.platform == "xiaohongshu"
    assert row.kol_uid == "k1"

    got = await service.get_cached_detail(user.id, session.id, "xiaohongshu", "k1")
    assert got is not None
    # 缓存行内的 payload 规范化 cache 块（hit=False + 行时间戳），保持 JSON 可序列化。
    assert got.payload_json["data"]["cache"]["hit"] is False
    assert got.payload_json["data"]["cache"]["fetched_at"] == "2026-01-01T12:00:00"
    assert got.evidence_refs_json == [
        {"artifact_path": "/data/metrics/followers", "sources": []}
    ]
    assert got.fetched_at == T0
    assert got.expires_at == T0 + timedelta(hours=24)
    assert not service.is_expired(got)


async def test_cache_key_unique_upsert(db_session, user_factory, session_factory) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    service = _make_service(db_session)

    await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )
    updated_payload = dict(PAYLOAD)
    updated_payload["data"] = dict(PAYLOAD["data"])
    updated_payload["data"]["identity"] = dict(PAYLOAD["data"]["identity"])
    updated_payload["data"]["identity"]["nickname"] = "改名达人"
    await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=updated_payload,
        evidence_refs=[{"artifact_path": "/data/trend/0/followers"}],
        fetched_at=T0 + timedelta(hours=1),
        expires_at=T0 + timedelta(hours=25),
    )

    count = await db_session.scalar(
        select(func.count(KolDetailCache.id)).where(
            KolDetailCache.user_id == user.id,
            KolDetailCache.session_id == session.id,
        )
    )
    assert count == 1
    row = await service.get_cached_detail(user.id, session.id, "xiaohongshu", "k1")
    assert row is not None
    assert row.payload_json["data"]["identity"]["nickname"] == "改名达人"
    assert row.expires_at == T0 + timedelta(hours=25)


async def test_cache_cross_session_isolation(db_session, user_factory, session_factory) -> None:
    user = await user_factory()
    session_a = await session_factory(user.id)
    session_b = await session_factory(user.id)
    service = _make_service(db_session)

    await service.set_cached_detail(
        user_id=user.id,
        session_id=session_a.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )

    assert await service.get_cached_detail(user.id, session_a.id, "xiaohongshu", "k1") is not None
    # 同一 (platform, kol_uid) 不同 Session：缓存按 (user, session) 归属，不跨会话命中。
    assert await service.get_cached_detail(user.id, session_b.id, "xiaohongshu", "k1") is None


async def test_cache_expiry_injected_clock(db_session, user_factory, session_factory) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    service = _make_service(db_session, now_fn=lambda: T0)

    row = await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )
    assert not service.is_expired(row)

    # 注入时钟越过 expires_at → 判定过期（服务允许刷新重新抓取）。
    service.now_fn = lambda: T0 + timedelta(hours=25)
    assert service.is_expired(row)


async def test_ttl_configurable(db_session) -> None:
    service = _make_service(db_session, ttl=6)
    assert service.cache_ttl_hours == 6
    default = _make_service(db_session)
    assert default.cache_ttl_hours == 24


async def test_set_cached_detail_recovers_from_concurrent_insert(
    db_session, user_factory, session_factory, monkeypatch
) -> None:
    """并发回填撞唯一约束（Fix 1）：捕获 IntegrityError 重读并更新，而不是 500。"""
    user = await user_factory()
    session = await session_factory(user.id)
    service = _make_service(db_session)
    # 并发赢家已插入缓存行。
    await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )
    real_get = service.get_cached_detail
    seen = {"n": 0}

    async def stale_get(user_id: str, session_id: str, platform: str, kol_uid: str):
        seen["n"] += 1
        if seen["n"] == 1:
            return None  # 竞态：本事务快照看不到赢家刚提交的行
        return await real_get(user_id, session_id, platform, kol_uid)

    monkeypatch.setattr(service, "get_cached_detail", stale_get)
    # 后插入者撞唯一约束 → 恢复：重读赢家行并更新。
    updated = await service.set_cached_detail(
        user_id=user.id,
        session_id=session.id,
        platform="xiaohongshu",
        kol_uid="k1",
        payload=PAYLOAD,
        evidence_refs=[{"artifact_path": "/data/trend/0/followers"}],
        fetched_at=T0,
        expires_at=T0 + timedelta(hours=25),
    )
    assert updated.evidence_refs_json == [{"artifact_path": "/data/trend/0/followers"}]
    assert updated.expires_at == T0 + timedelta(hours=25)
    # 没有 500、没有重复行。
    count = await db_session.scalar(
        select(func.count(KolDetailCache.id)).where(
            KolDetailCache.user_id == user.id,
            KolDetailCache.session_id == session.id,
        )
    )
    assert count == 1
