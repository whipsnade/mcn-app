"""达人详情轻量 Run 服务（设计 §13.2 / §8.1 / §12.1 / Task 17）。

点击圈选达人创建 ``kol_detail_v1`` 轻量 Run：
1. 先读取 Session 级 24h 缓存（``kol_detail_cache``，唯一
   ``(user_id, session_id, platform, kol_uid)``）；命中则零积分、零模型/MCP
   调用地重建 ``kol_detail_v2``（``data.cache.hit=true``）；
2. 未命中/过期才创建 ``run_kind=user``、``visibility=user``、
   ``profile=kol_detail_v1`` 的轻量 Run：模型经 Task 14 引擎（复用同一
   ``AgentEngine``，仅换 Profile）抓取 KOL 详情/热帖、构建 Draft、经 Reviewer
   发布 ``kol_detail_v2``，发布成功后回填缓存（payload + evidence refs +
   fetched_at/expires_at）。

并发车道：同一 Session 的 ``session_analyst_v1`` 与 ``kol_detail_v1`` 互不阻塞
（不同 artifact_key）；同一 ``(platform, kol_uid)`` 已存在活动 kol-detail Run
（持有 ``kol-detail:{platform}:{kol_uid}`` working head）时幂等返回现有 Run，
不重复创建。

TTL 默认 24h，由 ``KOL_DETAIL_CACHE_TTL_HOURS`` 配置；测试可注入 ``now_fn``
做确定性过期断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.keys import build_artifact_key
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    KolDetailCache,
)
from app.agent_artifacts.payloads.kol_detail import KolDetailV2
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import RunStatus
from app.model.contracts import ChatMessage


class KolDetailRunFailed(RuntimeError):
    """kol_detail_v1 Run 未成功产出 kol_detail_v2（未发布 / 状态异常 / 缺引擎）。"""


@dataclass(frozen=True)
class KolDetailRunSummary:
    """一次 ``create`` 的结果摘要。

    - 缓存命中：``cached=True``、``run_id=None``、``detail`` 带 ``cache.hit=true``；
    - 未命中且新建 Run：``cached=False``、``run_id`` 指向轻量 Run；
    - 已存在活动 kol-detail Run（幂等）：``cached=False``、``run_id`` 指向现有 Run，
      ``detail=None``（执行仍在进行）。
    """

    run_id: str | None
    detail: dict[str, Any] | None
    cached: bool
    version_id: str | None = None
    artifact_id: str | None = None


def _iso(value: Any) -> Any:
    """datetime/date → ISO 字符串，保证 JSON 可落库；字符串原样透传。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class KolDetailRunService:
    """达人详情轻量 Run 服务：缓存优先 + kol_detail_v1 引擎驱动 + 回填缓存。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        engine: AgentEngine | None,
        worker_id: str = "kol-detail-worker",
        lease_seconds: int = 300,
        cache_ttl_hours: int | None = None,
        now_fn: Callable[[], datetime] | None = None,
        model: str | None = None,
    ) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self.db = db
        self._engine = engine
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self.cache_ttl_hours = (
            cache_ttl_hours if cache_ttl_hours is not None else settings.kol_detail_cache_ttl_hours
        )
        self.now_fn = now_fn or utc_now
        self._model = model or settings.tencent_plan_model

    # ------------------------------------------------------------------ #
    # 缓存
    # ------------------------------------------------------------------ #

    async def get_cached_detail(
        self, user_id: str, session_id: str, platform: str, kol_uid: str
    ) -> KolDetailCache | None:
        """按 (user_id, session_id, platform, kol_uid) 读取缓存；不存在返回 None。"""
        return await self.db.scalar(
            select(KolDetailCache).where(
                KolDetailCache.user_id == user_id,
                KolDetailCache.session_id == session_id,
                KolDetailCache.platform == platform,
                KolDetailCache.kol_uid == kol_uid,
            )
        )

    def is_expired(self, cached: KolDetailCache) -> bool:
        """按当前时钟判定缓存是否过期（``expires_at`` 已到/已过即过期）。"""
        return cached.expires_at <= self.now_fn()

    async def set_cached_detail(
        self,
        *,
        user_id: str,
        session_id: str,
        platform: str,
        kol_uid: str,
        payload: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        fetched_at: datetime,
        expires_at: datetime,
    ) -> KolDetailCache:
        """写入/更新缓存（同键 upsert）；payload 的 cache 块与行时间戳保持一致。

        缓存行的 Evidence refs 与当前 Session 归属一致（§8.1）：缓存命中时
        从缓存 payload 重建，引用仍是本 Session 的不可变 Evidence。
        """
        row = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        payload = dict(payload or {})
        payload["data"] = dict((payload.get("data") or {}))
        payload["data"]["cache"] = {
            "hit": False,
            "fetched_at": _iso(fetched_at),
            "expires_at": _iso(expires_at),
        }
        if row is None:
            row = KolDetailCache(
                id=str(uuid4()),
                user_id=user_id,
                session_id=session_id,
                platform=platform,
                kol_uid=kol_uid,
                schema_version="kol_detail_v2",
                payload_json=payload,
                evidence_refs_json=evidence_refs,
                fetched_at=fetched_at,
                expires_at=expires_at,
            )
            self.db.add(row)
        else:
            row.schema_version = "kol_detail_v2"
            row.payload_json = payload
            row.evidence_refs_json = evidence_refs
            row.fetched_at = fetched_at
            row.expires_at = expires_at
        await self.db.flush()
        return row

    # ------------------------------------------------------------------ #
    # create
    # ------------------------------------------------------------------ #

    async def create(
        self,
        user_id: str,
        session_id: str,
        platform: str,
        kol_uid: str,
        selection_artifact_id: str | None = None,
        selection_version: str | None = None,
    ) -> KolDetailRunSummary:
        """缓存优先；未命中/过期才创建 kol_detail_v1 轻量 Run（§13.2）。"""
        cached = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        if cached is not None and not self.is_expired(cached):
            return self._summary_from_cache(cached)

        existing = await self._active_kol_detail_run(session_id, platform, kol_uid)
        if existing is not None:
            # 同一 (platform, kol_uid) 已有活动 kol-detail Run：幂等返回，不重复创建。
            return KolDetailRunSummary(run_id=existing, detail=None, cached=False)

        return await self._start_fresh_run(
            user_id,
            session_id,
            platform,
            kol_uid,
            selection_artifact_id=selection_artifact_id,
            selection_version=selection_version,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _summary_from_cache(self, cached: KolDetailCache) -> KolDetailRunSummary:
        """从缓存 payload 重建 kol_detail_v2：``data.cache.hit=true``，零模型/MCP 调用。"""
        payload = dict(cached.payload_json or {})
        payload["data"] = dict((payload.get("data") or {}))
        payload["data"]["cache"] = {
            "hit": True,
            "fetched_at": _iso(cached.fetched_at),
            "expires_at": _iso(cached.expires_at),
        }
        try:
            detail = KolDetailV2.model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            raise KolDetailRunFailed(f"invalid cached kol_detail_v2 payload: {exc}") from exc
        return KolDetailRunSummary(run_id=None, detail=detail, cached=True)

    async def _active_kol_detail_run(
        self, session_id: str, platform: str, kol_uid: str
    ) -> str | None:
        """查找同一 (platform, kol_uid) 是否已有活动 kol-detail Run（持有 working head）。

        working head 的 owner 只可能是 kol-detail Run（artifact_key 专属）；
        已发布/失败的 Draft owner 已释放，返回 None 允许新 Run 接管。
        """
        artifact_key = build_artifact_key("kol-detail", platform=platform, kol_uid=kol_uid)
        artifact = await self.db.scalar(
            select(AgentArtifact).where(
                AgentArtifact.session_id == session_id,
                AgentArtifact.artifact_key == artifact_key,
            )
        )
        if artifact is None:
            return None
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
        )
        if draft is not None and draft.owner_run_id is not None:
            return draft.owner_run_id
        return None

    async def _start_fresh_run(
        self,
        user_id: str,
        session_id: str,
        platform: str,
        kol_uid: str,
        *,
        selection_artifact_id: str | None,
        selection_version: str | None,
    ) -> KolDetailRunSummary:
        """创建 kol_detail_v1 用户 Run，驱动引擎至发布，发布成功后回填缓存。"""
        if self._engine is None:
            raise KolDetailRunFailed("no engine wired for kol_detail_v1 run")
        now = self.now_fn()
        run = AgentRun(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            run_kind="user",
            visibility="user",
            profile_name="kol_detail_v1",
            profile_version="v1",
            model=self._model,
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )
        self.db.add(run)
        await self.db.flush()

        repo = AgentRunRepository(self.db)
        attempt = await repo.begin_attempt(run.id)
        if not await repo.claim_lease(run.id, self._worker_id, self._lease_seconds):
            raise KolDetailRunFailed("kol_detail run could not acquire lease")

        messages = [
            ChatMessage(
                role="user",
                content=f"查看达人详情：platform={platform}, kol_uid={kol_uid}",
            )
        ]
        outcome = await self._engine.run(
            run=run,
            attempt_id=attempt.id,
            profile=get_profile("kol_detail_v1"),
            messages=messages,
        )
        if outcome.status != RunStatus.COMPLETED:
            raise KolDetailRunFailed(f"kol_detail_v1 run ended with status {outcome.status}")

        version = await self.db.scalar(
            select(AgentArtifactVersion)
            .where(AgentArtifactVersion.source_run_id == run.id)
            .order_by(AgentArtifactVersion.version.desc())
            .limit(1)
        )
        if version is None:
            raise KolDetailRunFailed(
                "kol_detail_v1 run completed but no kol_detail_v2 version was published"
            )
        payload = version.payload_json or {}
        try:
            detail = KolDetailV2.model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            raise KolDetailRunFailed(f"invalid published kol_detail_v2 payload: {exc}") from exc

        await self.set_cached_detail(
            user_id=user_id,
            session_id=session_id,
            platform=platform,
            kol_uid=kol_uid,
            payload=payload,
            evidence_refs=version.evidence_refs_json or [],
            fetched_at=now,
            expires_at=now + timedelta(hours=self.cache_ttl_hours),
        )
        return KolDetailRunSummary(
            run_id=run.id,
            detail=detail,
            cached=False,
            version_id=version.id,
            artifact_id=version.artifact_id,
        )


__all__ = ["KolDetailRunFailed", "KolDetailRunService", "KolDetailRunSummary"]
