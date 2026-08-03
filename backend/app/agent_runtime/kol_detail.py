"""达人详情轻量 Run 服务（设计 §13.2 / §8.1 / §12.1 / Task 17）。

点击圈选达人创建 ``kol_detail_v1`` 轻量 Run：
1. 先读取 Session 级 24h 缓存（``kol_detail_cache``，唯一
   ``(user_id, session_id, platform, kol_uid)``）；命中则零积分、零模型/MCP
   调用地重建 ``kol_detail_v2``（``data.cache.hit=true``）；
2. 缓存 miss 时回退最新已发布 ``kol_detail_v2`` Version（H2）：仍在 TTL
   内则由该 Version 的 payload + evidence refs 重建/刷新缓存行并按缓存
   命中返回（等价缓存命中，零模型/MCP/积分）；
3. 缓存与 Version 回退都不可用（过期/不存在/损坏）才创建
   ``run_kind=user``、``visibility=user``、``profile=kol_detail_v1`` 的
   轻量 Run：模型经 Task 14 引擎（复用同一 ``AgentEngine``，仅换 Profile）
   抓取 KOL 详情/热帖、构建 Draft、经 Reviewer 发布 ``kol_detail_v2``，
   发布成功后回填缓存（payload + evidence refs + fetched_at/expires_at）。

并发车道：同一 Session 的 ``session_analyst_v1`` 与 ``kol_detail_v1`` 互不阻塞
（不同 artifact_key）；同一 ``(platform, kol_uid)`` 已存在活动 kol-detail Run
（持有 ``kol-detail:{platform}:{kol_uid}`` working head）时幂等返回现有 Run，
不重复创建。

**同步执行（Task 19 评审文档项）**：``create`` 在缓存未命中且无活动 Run 时会在
**当前请求线程内同步**驱动 ``AgentEngine`` 到发布（含 MCP/模型调用），因此该 HTTP
请求会阻塞到达人详情 Run 出结果（最长到 Attempt 的 30 分钟 / 50 决策保护阈值），
而不是后台异步返回。这是 Task 17 的设计决策：响应直接携带可渲染的 detail
（cache hit）或 ``run_id`` + ``artifact_id``（fresh run）。前端应为此连接持有等待。

并发与恢复硬化（Code Review Fix 1/2 + Gate A G3）：
- 幂等检查对 working head 行 ``with_for_update`` 串行化并发 create，持有锁后
  重查缓存（并发写入者可能刚填好），避免重复创建 Run 造成重复 MCP/模型消耗；
- 只有「活动」（queued/running/reviewing）owner 才阻塞；paused/终态 owner
  无继续/恢复价值，释放 working head 让新 Run 接管，避免用户被卡死；
- **请求协调（G3）**：缓存未命中后、任何模型/MCP 调用发生**之前**，先在
  数据库建立 kol-detail 的 Artifact 身份 + working head（owner=新 Run）并
  立即提交（协调事务）。``(session_id, artifact_key)`` 唯一约束串行化同窗口
  并发：后到者的 INSERT 等待先到者提交后撞 IntegrityError，回滚自身协调事务
  并重读，幂等返回先到者的活动 Run（或其已回填的缓存）——两个真实并发
  create 最多一个进入引擎，MCP 抓取与积分扣费至多一次，首次 create 的并发
  窗口由此封闭（此前只能靠唯一约束 + ArtifactBusy + 缓存回填兜底）；
- 协调事务提交的 Run 处于 running + 本 worker 活跃租约：executor/recovery
  不会重复领取；崩溃超时后由恢复循环接管，transcript 经
  ``prompt_snapshot_json`` 的 platform/kol_uid 触发上下文恢复（G3 锚点）；
- 引擎失败收口：协调行已提交不能整单回滚——引擎正常收口（failed/paused/
  cancelled）时提交其终态；引擎抛出未捕获异常时尽力把 Run 置 failed、释放
  working head 并提交（不遮蔽原异常），下一次 create 立即可接管；
- **已发布 Version 回退（H2）**：发布时 ``publish_batch`` 先释放 working
  head 并提交 Version，缓存要等引擎返回后才回填——落在该窗口内的第二请求，
  以及 executor 崩溃恢复发布（不回填缓存）后的下一次点击，都会看到
  「Version 已发布 + 缓存为空」。缓存 miss 时改查最新已发布 Version：TTL
  内则由它重建缓存并按命中返回（零模型/MCP/积分），不再新建第二个 Run；
  Version 过期/不存在才走协调事务创建新 Run；payload 经
  ``KolDetailV2.model_validate`` 守卫，损坏则跳过回退走新 Run（不 500）；
- 缓存 payload 损坏时驱逐缓存行并刷新，而不是 500。

builder 的角色：``build_kol_detail_draft``（Task 17）不是运行时强制接线——
kol_detail_v1 由模型经引擎内联产出 kol_detail_v2 并经 Reviewer（Task 13）把关，
builder 供缓存命中重建与 Draft 工具做确定性转换。

TTL 默认 24h，由 ``KOL_DETAIL_CACHE_TTL_HOURS`` 配置；测试可注入 ``now_fn``
做确定性过期断言。
"""

from __future__ import annotations

import logging
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
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.model.contracts import ChatMessage

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "kol_detail_v2"

# ``prompt_snapshot_json`` 中 kol_detail 触发上下文的命名空间键（G3 恢复锚点）；
# 与 router 用于幂等的 ``idempotency_key``/``content_hash`` 顶层键互不冲突。
KOL_DETAIL_SNAPSHOT_KEY = "kol_detail"


def kol_detail_trigger_content(platform: str, kol_uid: str) -> str:
    """kol_detail Run 的触发消息文本：首次启动与崩溃恢复共用同一锚点（G3）。"""
    return f"查看达人详情：platform={platform}, kol_uid={kol_uid}"


def build_kol_detail_prompt_snapshot(
    *,
    platform: str,
    kol_uid: str,
    selection_artifact_id: str | None,
    selection_version: str | None,
) -> dict[str, Any]:
    """持久化 kol_detail Run 的触发上下文（无 input_message_id 时的恢复锚点）。

    kol_detail Run 由点击触发、没有 ``input_message_id``；崩溃接管时
    ``RunTranscriptLoader`` 从该快照恢复 platform/kol_uid 触发上下文，绝不
    回退到会话最近一条普通用户消息（可能是完全无关的意图）。
    """
    return {
        KOL_DETAIL_SNAPSHOT_KEY: {
            "platform": platform,
            "kol_uid": kol_uid,
            "selection_artifact_id": selection_artifact_id,
            "selection_version": selection_version,
        }
    }

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


@dataclass(frozen=True)
class _ClaimResult:
    """``_claim_working_head`` 的协调结果（G3）。

    - ``run_id``/``attempt_id`` 非空：本请求赢得协调权，Run + working head
      owner 已在协调事务提交，可进入引擎事务；
    - ``existing_run_id`` 非空：锁内发现活动 owner（TOCTOU 后到者），幂等返回；
    - ``lost_race``：同窗口并发撞 ``(session_id, artifact_key)`` 唯一约束，
      自身协调事务已整体回滚，调用方重读先到者状态后再决策。
    """

    run_id: str | None = None
    attempt_id: str | None = None
    existing_run_id: str | None = None
    lost_race: bool = False


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
        """缓存优先；已发布 Version 回退其次；都不可用才创建 kol_detail_v1
        轻量 Run（§13.2 + H2）。

        并发协调（G3）：缓存/Version 都未命中后、任何模型/MCP 调用发生
        **之前**，先在数据库建立/认领 kol-detail 的 working head（协调事务，
        立即提交）。两个真实并发 create 最多一个赢得协调权进入引擎——MCP
        抓取与积分扣费至多一次；后到者幂等返回先到者的活动 Run（或其已
        回填的缓存/刚发布的 Version）。
        """
        cached = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
        hit = await self._try_cache_hit(cached)
        if hit is not None:
            return hit
        # H2：缓存 miss 先回退最新已发布 Version（TTL 内等价缓存命中）——封闭
        # 「working head 已释放、缓存未回填」窗口与恢复发布不回填的重复扣费。
        hit = await self._try_published_version_hit(user_id, session_id, platform, kol_uid)
        if hit is not None:
            return hit

        # 协调循环：正常路径一轮完成。撞唯一约束（同窗口并发先到者已提交协调
        # 行）时重读——看到先到者的活动 Run / 它回填的缓存 / 它失败释放后的
        # 空 working head（此时由本请求接管，重新竞争）。
        for _ in range(2):
            # 幂等 + 串行化：锁住 working head（with_for_update），并发 create 在此串行。
            existing = await self._active_kol_detail_run(session_id, platform, kol_uid)
            if existing is not None:
                # 同一 (platform, kol_uid) 已有活动 kol-detail Run：幂等返回，不重复创建。
                return KolDetailRunSummary(run_id=existing, detail=None, cached=False)

            # 持有锁后重查：并发写入者可能已填好缓存或刚发布 Version（H2），
            # 避免重复抓取/模型/积分消耗。
            cached = await self.get_cached_detail(user_id, session_id, platform, kol_uid)
            hit = await self._try_cache_hit(cached)
            if hit is not None:
                return hit
            hit = await self._try_published_version_hit(user_id, session_id, platform, kol_uid)
            if hit is not None:
                return hit

            # 只有新建 Run 的路径才需要引擎（缓存命中/幂等返回不需要）。
            if self._engine is None:
                raise KolDetailRunFailed("no engine wired for kol_detail_v1 run")

            claim = await self._claim_working_head(
                user_id,
                session_id,
                platform,
                kol_uid,
                selection_artifact_id=selection_artifact_id,
                selection_version=selection_version,
            )
            if claim.lost_race:
                continue
            if claim.existing_run_id is not None:
                return KolDetailRunSummary(
                    run_id=claim.existing_run_id, detail=None, cached=False
                )
            if claim.run_id is not None and claim.attempt_id is not None:
                return await self._drive_fresh_run(
                    claim.run_id,
                    claim.attempt_id,
                    platform=platform,
                    kol_uid=kol_uid,
                )
        raise KolDetailRunFailed("kol_detail coordination could not be established")

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

    async def _try_published_version_hit(
        self, user_id: str, session_id: str, platform: str, kol_uid: str
    ) -> KolDetailRunSummary | None:
        """已发布 Version 回退（H2）：缓存 miss 时，最新已发布 ``kol_detail_v2``
        Version 仍在 TTL 内 → 由其 payload + evidence refs 重建缓存行并返回
        缓存命中摘要（等价缓存命中：零模型/MCP/积分，fetched_at/expires_at
        与 Version 发布时间对齐，语义同引擎回填）。

        封闭两个重复扣费窗口：
        - 发布窗口竞态：``publish_batch`` 先释放 working head 并提交 Version，
          缓存要等引擎返回后才回填——落在窗口内的第二请求直接命中刚发布的
          Version，不再新建第二个 Run；
        - 恢复不回填：executor 接管发布后不回填缓存，下次点击由 Version 重建。

        归属校验不放松：Artifact 按 session_id + artifact_key 查询（现有归属
        模式）；payload 形状沿用 ``KolDetailV2.model_validate`` 守卫。Version
        不存在/已过期/payload 损坏 → 返回 None 走新 Run 路径（不得 500）。
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
        version = await self.db.scalar(
            select(AgentArtifactVersion)
            .where(AgentArtifactVersion.artifact_id == artifact.id)
            .order_by(AgentArtifactVersion.version.desc())
            .limit(1)
        )
        if version is None:
            return None
        fetched_at = version.created_at
        expires_at = fetched_at + timedelta(hours=self.cache_ttl_hours)
        if expires_at <= self.now_fn():
            return None
        payload = version.payload_json or {}
        try:
            KolDetailV2.model_validate(payload)
        except ValidationError:
            # 发布边界有强类型校验，损坏 Version 不应存在；防御性跳过回退走新
            # Run（不 500），绝不把坏 payload 写进缓存。
            logger.warning(
                "kol_detail published version %s failed payload validation; "
                "skipping version fallback",
                version.id,
            )
            return None
        row = await self.set_cached_detail(
            user_id=user_id,
            session_id=session_id,
            platform=platform,
            kol_uid=kol_uid,
            payload=payload,
            evidence_refs=version.evidence_refs_json or [],
            fetched_at=fetched_at,
            expires_at=expires_at,
        )
        return self._summary_from_cache(row)

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

    async def _claim_working_head(
        self,
        user_id: str,
        session_id: str,
        platform: str,
        kol_uid: str,
        *,
        selection_artifact_id: str | None,
        selection_version: str | None,
    ) -> _ClaimResult:
        """协调事务（G3）：建立/认领 kol-detail 的 working head 并**立即提交**。

        - Artifact 身份不存在时创建：``(session_id, artifact_key)`` 唯一约束
          串行化同窗口并发——后到者的 INSERT 等待先到者提交后撞
          ``IntegrityError``，整体回滚自身协调事务（其 Run 一并消失），由
          调用方重读幂等返回先到者的活动 Run；
        - Artifact 已存在时只对 working head 行 ``with_for_update``（**不显式
          锁 Artifact 行**：``publish_batch`` 的加锁顺序是 Draft → Artifact，
          反向加锁会与之死锁）——锁内重判 owner：活动 owner 幂等返回；非活动
          owner 释放后由本请求接管（与 Fix 2 同语义）；
        - 赢得协调权：同事务创建 queued Run（含 ``prompt_snapshot_json`` 触发
          上下文，G3 恢复锚点）→ ``begin_attempt``（→running）→ ``claim_lease``
          → working head owner 置为该 Run，一次提交。提交后 Run 处于
          running + 本 worker 活跃租约：executor/recovery 不会重复领取；
          进程崩溃超时后由恢复循环接管（transcript 经 prompt_snapshot 恢复
          触发上下文）。
        """
        artifact_key = build_artifact_key("kol-detail", platform=platform, kol_uid=kol_uid)
        now = utc_now()
        artifact = await self.db.scalar(
            select(AgentArtifact).where(
                AgentArtifact.session_id == session_id,
                AgentArtifact.artifact_key == artifact_key,
            )
        )
        if artifact is None:
            artifact = AgentArtifact(
                session_id=session_id,
                user_id=user_id,
                module="kol-detail",
                artifact_type=SCHEMA_VERSION,
                artifact_key=artifact_key,
                status="draft",
                latest_version=0,
                activity_sequence=0,
                created_at=now,
                updated_at=now,
            )
            self.db.add(artifact)
            try:
                await self.db.flush()
            except IntegrityError:
                # 同窗口并发：先到者已提交协调行。整体回滚自身协调事务（新建
                # 的 Run 一并消失），调用方重读幂等返回先到者的活动 Run。
                await self.db.rollback()
                return _ClaimResult(lost_race=True)

        draft = await self.db.scalar(
            select(ArtifactDraft)
            .where(ArtifactDraft.artifact_id == artifact.id)
            .with_for_update()
        )
        if draft is not None and draft.owner_run_id is not None:
            if await self._owner_is_active(draft.owner_run_id):
                # 锁内权威判定：已有活动 owner（TOCTOU 后到的并发请求），幂等返回。
                await self.db.commit()
                return _ClaimResult(existing_run_id=draft.owner_run_id)
            # 非活动 owner（paused/终态/消失）：释放 working head 后接管（Fix 2 同语义）。
            await self._service.release_draft(draft.id, outcome="failed")
            await self.db.flush()

        run = AgentRun(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            run_kind="user",
            visibility="user",
            profile_name="kol_detail_v1",
            profile_version="v1",
            model=self._model,
            prompt_snapshot_json=build_kol_detail_prompt_snapshot(
                platform=platform,
                kol_uid=kol_uid,
                selection_artifact_id=selection_artifact_id,
                selection_version=selection_version,
            ),
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
            # pragma: no cover - 新建 Run 无租约必然可领取；防御性整体回滚。
            await self.db.rollback()
            raise KolDetailRunFailed("kol_detail run could not acquire lease")
        if draft is None:
            draft = ArtifactDraft(
                artifact_id=artifact.id,
                session_id=session_id,
                owner_run_id=run.id,
                current_revision=0,
                status="drafting",
                review_count=0,
                revision_count=0,
                updated_at=now,
            )
            self.db.add(draft)
        else:
            draft.owner_run_id = run.id
            draft.status = "drafting"
            draft.updated_at = now
        await self.db.flush()
        await self.db.commit()
        return _ClaimResult(run_id=run.id, attempt_id=attempt.id)

    async def _drive_fresh_run(
        self,
        run_id: str,
        attempt_id: str,
        *,
        platform: str,
        kol_uid: str,
    ) -> KolDetailRunSummary:
        """驱动已持有协调行的 Run 至发布，发布成功后回填缓存。

        引擎在每个事件 append 处增量提交（``AgentEventStream.append`` 是提交
        点），协调行（Run/Artifact/Draft）更已在协调事务提交——失败收口不能
        依赖整体回滚：引擎正常收口（failed/paused/cancelled）时提交其终态；
        引擎抛出未捕获异常时尽力把 Run 置 failed、释放 working head 并提交
        （不遮蔽原异常），保证下一次 create 能立即接管、绝不 artifact_busy。
        """
        engine = self._engine
        if engine is None:  # pragma: no cover - create 已先行校验；防御与类型收窄。
            raise KolDetailRunFailed("no engine wired for kol_detail_v1 run")
        run = await self.db.get(AgentRun, run_id)
        if run is None:  # pragma: no cover - 协调事务刚提交必然可读
            raise KolDetailRunFailed("kol_detail run not readable after claim")
        now = self.now_fn()
        trigger = kol_detail_trigger_content(platform, kol_uid)
        messages = [ChatMessage(role="user", content=trigger)]
        try:
            outcome = await engine.run(
                run=run,
                attempt_id=attempt_id,
                profile=get_profile("kol_detail_v1"),
                messages=messages,
                # §5.8/§10.5：kol_detail Run 是用户可见 Run，注入 thinking sink。
                thinking_sink=engine.thinking_sink_for(run),
                # G3：显式用户问题锚点，不经消息列表反推。
                user_question=trigger,
            )
        except Exception:
            await self.db.rollback()
            await self._settle_failed_run(run_id)
            raise
        if outcome.status != RunStatus.COMPLETED:
            # 引擎出口已自行收口（failed/paused/cancelled：迁移终态、释放
            # Draft、清租约）——提交这些收尾，避免已提交的协调行悬挂在
            # running（否则会被恢复循环误接管重放）。
            await self._release_working_head_if_owned(run_id)
            await self.db.commit()
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
            user_id=run.user_id,
            session_id=run.session_id,
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

    async def _release_working_head_if_owned(self, run_id: str) -> None:
        """防御性释放：working head 仍挂在该 Run 名下时置 failed 并释放 owner。"""
        drafts = (
            await self.db.scalars(
                select(ArtifactDraft).where(ArtifactDraft.owner_run_id == run_id)
            )
        ).all()
        for draft in drafts:
            await self._service.release_draft(draft.id, outcome="failed")

    async def _settle_failed_run(self, run_id: str) -> None:
        """引擎抛出未捕获异常后的尽力收口（不遮蔽原异常，G3）。

        协调行（Run/Artifact/Draft）已在协调事务提交：把 Run 置 failed、
        释放 working head、补发 ``run.failed`` 终态事件并提交，下一次
        create 立即可接管。收口本身失败时只记日志——Run 保持 running +
        租约，恢复循环在租约过期后接管自愈（transcript 经
        ``prompt_snapshot_json`` 的 platform/kol_uid 触发上下文恢复）。
        """
        try:
            repo = AgentRunRepository(self.db)
            try:
                await repo.transition(run_id, RunStatus.FAILED, worker_id=self._worker_id)
                failed = True
            except InvalidRunTransition:
                # 租约过期/被接管或已是终态：系统级收口（他人活跃持有时返回
                # False，终态事件由接管方负责，A4 闸门同语义）。
                failed = await repo.force_fail(run_id, error_code="kol_detail_error")
            if failed:
                await self._release_working_head_if_owned(run_id)
                run = await self.db.get(AgentRun, run_id)
                if run is not None:
                    # 一次性 broker 仅为持久化终态事件（罕见系统异常路径，实时
                    # 推送缺失由 HTTP 500 与重连重放兜底）。
                    await AgentEventStream(
                        self.db, AgentEventBroker()
                    ).append_terminal_once(
                        run_id,
                        run.user_id,
                        "run.failed",
                        {"outcome": "failed", "error_code": "kol_detail_error"},
                    )
            await self.db.commit()
        except Exception:
            logger.exception("kol_detail run %s failure settlement failed", run_id)


__all__ = [
    "KOL_DETAIL_SNAPSHOT_KEY",
    "KolDetailRunFailed",
    "KolDetailRunService",
    "KolDetailRunSummary",
    "build_kol_detail_prompt_snapshot",
    "kol_detail_trigger_content",
]
