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

**同步执行（Task 19 评审文档项）**：``create`` 在缓存未命中且无活动 Run 时会在
**当前请求线程内同步**驱动 ``AgentEngine`` 到发布（含 MCP/模型调用），因此该 HTTP
请求会阻塞到达人详情 Run 出结果（最长到 Attempt 的 30 分钟 / 50 决策保护阈值），
而不是后台异步返回。这是 Task 17 的设计决策：响应直接携带可渲染的 detail
（cache hit）或 ``run_id`` + ``artifact_id``（fresh run）。前端应为此连接持有等待。

并发与恢复硬化（Code Review Fix 1/2）：
- 幂等检查对 working head 行 ``with_for_update`` 串行化并发 create，持有锁后
  重查缓存（并发写入者可能刚填好），避免重复创建 Run 造成重复 MCP/模型消耗；
- 只有「活动」（queued/running/reviewing）owner 才阻塞；paused/终态 owner
  无继续/恢复价值，释放 working head 让新 Run 接管，避免用户被卡死；
- 首次 create（artifact/draft 尚不存在）的并发窗口在无迁移下无法完全封闭，
  由 ``(session_id, artifact_key)`` 唯一约束 + 引擎 ArtifactBusy +
  ``set_cached_detail`` 的 IntegrityError 恢复共同兜底；
- 缓存 payload 损坏时驱逐缓存行并刷新，而不是 500。

builder 的角色：``build_kol_detail_draft``（Task 17）不是运行时强制接线——
kol_detail_v1 由模型经引擎内联产出 kol_detail_v2 并经 Reviewer（Task 13）把关，
builder 供缓存命中重建与 Draft 工具做确定性转换。

TTL 默认 24h，由 ``KOL_DETAIL_CACHE_TTL_HOURS`` 配置；测试可注入 ``now_fn``
做确定性过期断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.keys import build_artifact_key
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    KolDetailCache,
)
from app.agent_artifacts.payloads.kol_detail import KolDetailV2
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import RunStatus
from app.model.contracts import ChatMessage

SCHEMA_VERSION = "kol_detail_v2"

# 已暂停/终态的 owner 不再视为「活动」：不再阻塞新 Run，允许其接管 working head。
_NON_ACTIVE_OWNER_STATUSES = frozenset(
    {
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


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
        self._service = ArtifactService(db)

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

        并发回填的 IntegrityError 兜底（Fix 1）：两个并发 create 都 miss 缓存、
        都走到回填时，先插入者成功、后插入者撞唯一约束——捕获后重读并更新，
        而不是对用户 500（MySQL 重复键不中止事务，可继续）。
        """
        normalized = self._normalize_cache_payload(payload, fetched_at, expires_at)
        row = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        if row is not None:
            self._update_cache_row(row, normalized, evidence_refs, fetched_at, expires_at)
            await self.db.flush()
            return row
        try:
            await self.db.execute(
                insert(KolDetailCache).values(
                    id=str(uuid4()),
                    user_id=user_id,
                    session_id=session_id,
                    platform=platform,
                    kol_uid=kol_uid,
                    schema_version=SCHEMA_VERSION,
                    payload_json=normalized,
                    evidence_refs_json=evidence_refs,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
            )
        except IntegrityError:
            winner = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
            if winner is None:  # pragma: no cover - 竞态窗口内行必然已存在
                raise
            self._update_cache_row(winner, normalized, evidence_refs, fetched_at, expires_at)
            await self.db.flush()
            return winner
        await self.db.flush()
        row = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        if row is None:  # pragma: no cover - 刚插入必然可读
            raise KolDetailRunFailed("cache row not readable after insert")
        return row

    def _normalize_cache_payload(
        self, payload: dict[str, Any], fetched_at: datetime, expires_at: datetime
    ) -> dict[str, Any]:
        normalized = dict(payload or {})
        normalized["data"] = dict((normalized.get("data") or {}))
        normalized["data"]["cache"] = {
            "hit": False,
            "fetched_at": _iso(fetched_at),
            "expires_at": _iso(expires_at),
        }
        return normalized

    @staticmethod
    def _update_cache_row(
        row: KolDetailCache,
        payload: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        row.schema_version = SCHEMA_VERSION
        row.payload_json = payload
        row.evidence_refs_json = evidence_refs
        row.fetched_at = fetched_at
        row.expires_at = expires_at

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
        hit = await self._try_cache_hit(cached)
        if hit is not None:
            return hit

        # 幂等 + 串行化：锁住 working head（with_for_update），并发 create 在此串行。
        existing = await self._active_kol_detail_run(session_id, platform, kol_uid)
        if existing is not None:
            # 同一 (platform, kol_uid) 已有活动 kol-detail Run：幂等返回，不重复创建。
            return KolDetailRunSummary(run_id=existing, detail=None, cached=False)

        # 持有锁后重查缓存：并发写入者可能已填好缓存，避免重复抓取/模型消耗。
        cached = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        hit = await self._try_cache_hit(cached)
        if hit is not None:
            return hit

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

    async def _try_cache_hit(
        self, cached: KolDetailCache | None
    ) -> KolDetailRunSummary | None:
        """缓存可用时返回命中摘要；过期/损坏返回 None（损坏时驱逐缓存行）。

        损坏的缓存 payload 不再对用户 500：驱逐该行并让 create 落到刷新路径。
        """
        if cached is None or self.is_expired(cached):
            return None
        try:
            return self._summary_from_cache(cached)
        except KolDetailRunFailed:
            await self.db.delete(cached)
            await self.db.flush()
            return None

    async def _active_kol_detail_run(
        self, session_id: str, platform: str, kol_uid: str
    ) -> str | None:
        """查找同一 (platform, kol_uid) 是否已有活动 kol-detail Run（持有 working head）。

        - 对 working head 行 ``with_for_update``：并发 create 在此串行，后到者
          看到先到者的活动 Run（Fix 1）；
        - working head 的 owner 只可能是 kol-detail Run（artifact_key 专属）；
        - paused/终态 owner（无继续/恢复价值）不再阻塞：释放 working head 让
          新 Run 接管，避免用户被卡死（Fix 2）；
        - 已发布/失败的 Draft owner 已释放，返回 None 允许新 Run 接管。
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
            select(ArtifactDraft)
            .where(ArtifactDraft.artifact_id == artifact.id)
            .with_for_update()
        )
        if draft is None or draft.owner_run_id is None:
            return None
        if not await self._owner_is_active(draft.owner_run_id):
            await self._service.release_draft(draft.id, outcome="failed")
            await self.db.flush()
            return None
        return draft.owner_run_id

    async def _owner_is_active(self, run_id: str) -> bool:
        """owner Run 是否仍活动（queued/running/reviewing）；缺失/暂停/终态视为非活动。"""
        run = await self.db.get(AgentRun, run_id)
        if run is None:
            return False
        return RunStatus(run.status) not in _NON_ACTIVE_OWNER_STATUSES

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
            # §5.8/§10.5：kol_detail Run 是用户可见 Run，注入 thinking sink。
            thinking_sink=self._engine.thinking_sink_for(run),
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
