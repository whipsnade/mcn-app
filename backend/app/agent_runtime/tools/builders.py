"""Artifact Builder 工具（v3 加固 §3.3/§6.1，B2）。

五个 Builder 工具把「模型手写带 lineage 的大型正式 payload」替换为确定性
转换：模型只提供用户确认的 scope、Evidence ID（按章节分组）与叙事字段；
工具按 ID 读取当前 Session 的 Evidence（归属校验，跨 Session 一律
``evidence_not_found`` 不泄漏存在性）、调用 ``agent_artifacts.builders``
的确定性 Builder、经 :class:`ArtifactService` 落 Draft。

输出契约（§6.1）：只回 ``artifact_id/draft_id/revision_id/schema_version``
+ 受限章节/limitation 摘要，**不把完整 payload 回灌模型上下文**——payload
可经 ``read_artifact``/Draft 查询按需读取。

工具只做转换与持久化：不选择 MCP 工具、不发起外部查询、不改变用户目标。
Evidence 不足 / ID 无效 / 归属失败 / payload 不过审都是结构化错误回喂模型；
参数或 scope 的 Pydantic 校验失败（含模型编造字段）由基座 ``execute``
统一转为 ``draft_build_error`` 字段级明细（截断到上限），只有未知异常才
冒泡为 engine 级失败。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.builders.brand import (
    EVIDENCE_GROUPS as BRAND_EVIDENCE_GROUPS,
)
from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import (
    EVIDENCE_GROUPS as CAMPAIGN_EVIDENCE_GROUPS,
)
from app.agent_artifacts.builders.campaign import build_campaign_report_draft
from app.agent_artifacts.builders.common import DraftBuildError, DraftBuildResult
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft
from app.agent_artifacts.builders.raw_rows import extract_rows, unwrap_payload
from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadInvalid
from app.agent_runtime.models import AgentSession, EvidenceItem
from app.agent_runtime.tools.artifacts import kol_detail_snapshot_selection_parent
from app.agent_runtime.tools.contracts import ToolContext, ToolResult

NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
EVIDENCE_NOT_FOUND = "evidence_not_found"
DRAFT_BUILD_ERROR = "draft_build_error"

# 结构化错误回喂的长度上限：字段级明细足够模型定位问题即可，绝不撑爆上下文。
_ERROR_SUMMARY_LIMIT = 2000


def _failed(error_type: str, message: str) -> ToolResult:
    return ToolResult(status="failed", safe_summary=message, error_type=error_type)


def _truncate(text: str, limit: int = _ERROR_SUMMARY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic 校验失败 → 字段级明细（``loc: msg [type]``），截断到上限。"""
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {error.get('msg')} [{error.get('type')}]")
    return _truncate("invalid builder arguments: " + "; ".join(parts))


def _draft_summary(
    result: DraftBuildResult,
    *,
    artifact_id: str,
    artifact_key: str,
    draft_id: str,
    revision_id: str,
    revision: int,
) -> str:
    """成功输出：身份 + 受限摘要；绝不包含 payload 本体（不回灌模型上下文）。"""
    payload = result.payload
    restricted = {
        section: entry
        for section, entry in (payload.get("availability") or {}).items()
        if entry.get("status") != "complete"
    }
    return json.dumps(
        {
            "artifact_id": artifact_id,
            "artifact_key": artifact_key,
            "draft_id": draft_id,
            "revision_id": revision_id,
            "revision": revision,
            "schema_version": result.schema_version,
            "data_status": payload.get("data_status"),
            "restricted_sections": restricted,
            "limitations": [
                {"code": item.get("code"), "message": item.get("message")}
                for item in (payload.get("limitations") or [])
            ],
        },
        ensure_ascii=False,
    )


class _BuilderToolBase:
    """Builder 工具共享基座：DB 归属校验、Evidence 读取、Draft 持久化。

    ``execute`` 统一把可预期失败（参数/scope 的 ``ValidationError``、builder
    领域异常 ``DraftBuildError``）转为结构化 ``draft_build_error`` 回喂模型
    （字段级明细、截断到上限）——模型据此修正参数自愈；只有未知异常才冒泡
    为 engine 级 ``failed unexpectedly``。子类实现 ``_execute``。
    """

    points_cost = 0
    external_side_effect = True

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        try:
            return await self._execute(context, arguments)
        except ValidationError as exc:
            return _failed(DRAFT_BUILD_ERROR, _format_validation_error(exc))
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, _truncate(str(exc)))

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        raise NotImplementedError

    async def _check_session(self, context: ToolContext) -> ToolResult | None:
        session = await self._db.get(AgentSession, context.session_id)
        if session is None:
            return _failed(NOT_FOUND, "session_not_found")
        if session.user_id != context.user_id:
            return _failed(FORBIDDEN, "session_forbidden")
        return None

    async def _load_evidence(
        self, context: ToolContext, evidence_id: str
    ) -> EvidenceItem | ToolResult:
        """按 ID 读取 Evidence；缺失或跨 Session 一律 evidence_not_found。"""
        item = await self._db.get(EvidenceItem, evidence_id)
        if item is None or item.session_id != context.session_id:
            return _failed(
                EVIDENCE_NOT_FOUND,
                f"evidence not found in current session: {evidence_id!r}",
            )
        return item

    async def _load_evidence_groups(
        self, context: ToolContext, groups: dict[str, list[str]]
    ) -> dict[str, list[tuple[str, Any]]] | ToolResult:
        """按分组加载 Evidence，返回 builder 消费的 ``(evidence_id, raw_payload)``。"""
        loaded: dict[str, list[tuple[str, Any]]] = {}
        for group, ids in groups.items():
            pairs: list[tuple[str, Any]] = []
            for evidence_id in ids:
                item = await self._load_evidence(context, evidence_id)
                if isinstance(item, ToolResult):
                    return item
                pairs.append((item.id, item.raw_payload_json))
            loaded[group] = pairs
        return loaded

    async def _persist(
        self, context: ToolContext, result: DraftBuildResult
    ) -> ToolResult:
        """经 ArtifactService 落 Draft（稳定身份复用即追加新 Revision）。"""
        try:
            artifact, draft, revision = await ArtifactService(self._db).create_or_get_draft(
                session_id=context.session_id,
                user_id=context.user_id,
                run_id=context.run_id,
                module=result.module,
                business_fields=result.business_fields,
                schema_version=result.schema_version,
                payload=result.payload,
                evidence_refs=result.evidence_refs,
                artifact_type=result.artifact_type,
                parent_artifact_id=result.parent_artifact_id,
                parent_artifact_version_id=result.parent_artifact_version_id,
            )
        except ArtifactBusy as exc:
            return _failed(exc.code, str(exc))
        except ArtifactPayloadInvalid as exc:
            return _failed(exc.code, str(exc))
        return ToolResult(
            status="success",
            safe_summary=_draft_summary(
                result,
                artifact_id=artifact.id,
                artifact_key=artifact.artifact_key,
                draft_id=draft.id,
                revision_id=revision.id,
                revision=revision.revision,
            ),
        )


# ---------------------------------------------------------------------------
# build_brand_report_draft
# ---------------------------------------------------------------------------


class BuildBrandReportDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict[str, Any]
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    narrative: dict[str, Any] | None = None
    top_posts_limit: int = Field(default=20, ge=1, le=20)


class BuildBrandReportDraftTool(_BuilderToolBase):
    """构建品牌报告 Draft（brand_report_v3，确定性聚合 + 字段级 lineage）。"""

    description = (
        "把已采集的 Evidence 确定性聚合为品牌报告 Draft（brand_report_v3）。"
        "scope={brand, period{start,end,timezone}, platforms[], keywords[], "
        "comparison_mode:none|mom|mom_yoy}；evidence 按章节分组（键："
        + "/".join(BRAND_EVIDENCE_GROUPS)
        + "），值为当前会话的 evidence_id 列表；mom/mom_yoy 请求的对比期由"
        "overview_mom/overview_yoy 分组提供同口径基线数据。narrative 可选"
        "（executive_summary/findings[]/recommendations[]，条目的 "
        "supporting_paths 必须指向 data 内路径）。输出只含 "
        "artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        'scope={"brand":"瑞幸咖啡","period":{"start":"2026-07-01","end":"2026-07-31",'
        '"timezone":"Asia/Shanghai"},"platforms":["xiaohongshu"],"keywords":["瑞幸"],'
        '"comparison_mode":"none"}；'
        'evidence={"overview_current":["ev-1"],"sentiment":["ev-2"]}；'
        'narrative={"executive_summary":"...","findings":[{"title":"...","detail":"...",'
        '"supporting_paths":["data.overview.total_volume"]}],"recommendations":[{"title":"...",'
        '"action":"...","rationale":"...","supporting_paths":["data.topics"]}]}。'
        "注意：findings 条目字段是 title/detail（不是 description）；recommendations "
        "条目必须含 title/action/rationale；supporting_paths 是 data 下真实存在的"
        "点分路径（data. 前缀可省略）。参数或 scope 校验失败返回 draft_build_error "
        "字段级明细，按明细修正后重试。"
    )

    name = "build_brand_report_draft"
    input_model = BuildBrandReportDraftArgs

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildBrandReportDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_brand_report_draft requires a database session")
        unknown_groups = sorted(set(args.evidence) - set(BRAND_EVIDENCE_GROUPS))
        if unknown_groups:
            return _failed(
                DRAFT_BUILD_ERROR,
                f"unknown evidence groups: {unknown_groups}; "
                f"allowed: {list(BRAND_EVIDENCE_GROUPS)}",
            )
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error
        groups = await self._load_evidence_groups(context, args.evidence)
        if isinstance(groups, ToolResult):
            return groups
        try:
            result = build_brand_report_draft(
                scope=args.scope,
                evidence=groups,
                narrative=args.narrative,
                top_posts_limit=args.top_posts_limit,
                source_names=("brand_evidence",),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, str(exc))
        return await self._persist(context, result)


# ---------------------------------------------------------------------------
# build_campaign_report_draft
# ---------------------------------------------------------------------------


class BuildCampaignReportDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict[str, Any]
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    narrative: dict[str, Any] | None = None
    top_posts_limit: int = Field(default=20, ge=1, le=20)


class BuildCampaignReportDraftTool(_BuilderToolBase):
    """构建活动报告 Draft（campaign_report_v2，确定性聚合 + 字段级 lineage）。"""

    description = (
        "把已采集的原始帖 Evidence 确定性聚合为活动报告 Draft（campaign_report_v2）。"
        "scope={brand, campaign, period{start,end,timezone}, platforms[], keywords[]}；"
        "evidence 分组（键："
        + "/".join(CAMPAIGN_EVIDENCE_GROUPS)
        + "）：posts 为原始帖行（必需章节主数据源），sentiment 为可选情感明细行"
        "（缺失时用 posts 行情感字段）。narrative 可选（executive_summary/"
        "phase_review[]/findings[]/recommendations[]，supporting_paths 必须指向"
        " data 内路径）。输出只含 artifact_id/draft_id/revision_id/schema_version"
        " 与受限摘要。"
        "输入契约示例："
        'scope={"brand":"瑞幸咖啡","campaign":"生椰拿铁上新","period":{"start":"2026-07-01",'
        '"end":"2026-07-31","timezone":"Asia/Shanghai"},"platforms":["xiaohongshu"],'
        '"keywords":["生椰拿铁"]}；'
        'evidence={"posts":["ev-1"],"sentiment":["ev-2"]}；'
        'narrative={"executive_summary":"...","phase_review":[{"phase":"预热期","detail":"...",'
        '"supporting_paths":["data.daily_trend"]}],"findings":[{"title":"...","detail":"...",'
        '"supporting_paths":["data.overview.total_engagement"]}],"recommendations":[{"title":"...",'
        '"action":"...","rationale":"...","supporting_paths":["data.top_posts"]}]}。'
        "注意：phase_review 条目为 {phase, detail, supporting_paths}；findings 条目字段是 "
        "title/detail（不是 description）；recommendations 条目必须含 title/action/rationale；"
        "supporting_paths 是 data 下真实存在的点分路径。校验失败返回 draft_build_error "
        "字段级明细，按明细修正后重试。"
    )

    name = "build_campaign_report_draft"
    input_model = BuildCampaignReportDraftArgs

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildCampaignReportDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_campaign_report_draft requires a database session")
        unknown_groups = sorted(set(args.evidence) - set(CAMPAIGN_EVIDENCE_GROUPS))
        if unknown_groups:
            return _failed(
                DRAFT_BUILD_ERROR,
                f"unknown evidence groups: {unknown_groups}; "
                f"allowed: {list(CAMPAIGN_EVIDENCE_GROUPS)}",
            )
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error
        groups = await self._load_evidence_groups(context, args.evidence)
        if isinstance(groups, ToolResult):
            return groups
        try:
            result = build_campaign_report_draft(
                scope=args.scope,
                evidence=groups,
                narrative=args.narrative,
                top_posts_limit=args.top_posts_limit,
                source_names=("campaign_evidence",),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, str(exc))
        return await self._persist(context, result)


# ---------------------------------------------------------------------------
# build_kol_selection_draft
# ---------------------------------------------------------------------------


class BuildKolSelectionDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict[str, Any]
    evidence_id: str = Field(min_length=1)


class BuildKolSelectionDraftTool(_BuilderToolBase):
    """构建 KOL 圈选名单 Draft（kol_selection_v3；评分委托 rank_kols/kol_score_v2）。"""

    description = (
        "把 KOL 列表 Evidence 确定性转换为圈选名单 Draft（kol_selection_v3）。"
        "scope 为圈选条件对象（brand/category/campaign 可空、platforms[]、"
        "audience{regions[],age_ranges[],interests[]}、filters 预算/粉丝门槛）；"
        "evidence_id 为当前会话的 KOL 列表证据（列表行含 platform/kol_uid/"
        "nickname/followers/engagement_total/score_inputs 等）。评分由确定性 "
        "rank_kols（kol_score_v2 八维）完成，默认跨平台 Top20 按互动量降序。"
        "输出只含 artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        'scope={"brand":null,"category":"美食","campaign":null,"platforms":["小红书"],'
        '"audience":{"regions":["上海"],"age_ranges":["18-24"],"interests":["美食"]},'
        '"filters":{"budget_min":null,"budget_max":100000,"follower_min":10000,'
        '"follower_max":null}}；'
        'evidence_id="<当前会话 KOL 列表证据 id>"。'
        "注意：audience 与 filters 必填；filters 只有 budget_min/budget_max/"
        "follower_min/follower_max 四个字段（不存在 follower_threshold 等其他字段），"
        "多传或错传字段返回 draft_build_error 字段级明细，按明细修正后重试。"
    )

    name = "build_kol_selection_draft"
    input_model = BuildKolSelectionDraftArgs

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildKolSelectionDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_kol_selection_draft requires a database session")
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error
        item = await self._load_evidence(context, args.evidence_id)
        if isinstance(item, ToolResult):
            return item
        items = [ref.row for ref in extract_rows(item.id, item.raw_payload_json)]
        if not items:
            return _failed(
                DRAFT_BUILD_ERROR,
                f"evidence {args.evidence_id!r} does not contain a KOL item list",
            )
        try:
            result = await build_kol_selection_draft(
                scope=args.scope,
                evidence_id=item.id,
                items=items,
                context=context,
                db=self._db,
                source_names=(item.source_name,),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, str(exc))
        return await self._persist(context, result)


# ---------------------------------------------------------------------------
# build_kol_analysis_draft
# ---------------------------------------------------------------------------


class BuildKolAnalysisDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_artifact_id: str = Field(min_length=1)
    selection_version: int | None = Field(default=None, ge=1)
    analysis_period: str | None = None


class BuildKolAnalysisDraftTool(_BuilderToolBase):
    """对已发布圈选名单 Version 构建 KOL 分析 Draft（kol_analysis_v2）。"""

    description = (
        "对已发布的圈选名单（kol_selection_v3 Version）做组合分析，产出 "
        "kol_analysis_v2 Draft。selection_artifact_id 为当前会话的名单 "
        "Artifact id；selection_version 缺省取最新已发布版本。分析数据引用"
        "名单 Version 并递归追溯其 Evidence。输出只含 "
        "artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        '{"selection_artifact_id":"<名单 Artifact id>","selection_version":1,'
        '"analysis_period":"2026-07"}（后两个参数可省略）。'
        "名单未发布或 Artifact 不属于当前会话返回 not_found；其他校验失败返回 "
        "draft_build_error 字段级明细，按明细修正后重试。"
    )

    name = "build_kol_analysis_draft"
    input_model = BuildKolAnalysisDraftArgs

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildKolAnalysisDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_kol_analysis_draft requires a database session")
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error

        artifact = await self._db.get(AgentArtifact, args.selection_artifact_id)
        if (
            artifact is None
            or artifact.session_id != context.session_id
            or artifact.module != "kol-selection"
        ):
            return _failed(NOT_FOUND, "selection_artifact_not_found")
        version_no = (
            args.selection_version
            if args.selection_version is not None
            else artifact.latest_version
        )
        version = await self._db.scalar(
            select(AgentArtifactVersion).where(
                AgentArtifactVersion.artifact_id == artifact.id,
                AgentArtifactVersion.version == version_no,
            )
        )
        if version is None:
            return _failed(
                NOT_FOUND,
                f"selection artifact {artifact.id!r} has no published version {version_no}",
            )
        try:
            result = build_kol_analysis_draft(
                selection_artifact_id=artifact.id,
                selection_payload=version.payload_json or {},
                parent_artifact_version_id=version.id,
                selection_version=str(version.version),
                analysis_period=args.analysis_period,
                selection_refs=version.evidence_refs_json or [],
                source_names=("kol_selection_v3",),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, str(exc))
        return await self._persist(context, result)


# ---------------------------------------------------------------------------
# build_kol_detail_draft
# ---------------------------------------------------------------------------


class BuildKolDetailDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1)
    kol_uid: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    cache_state: dict[str, Any] = Field(default_factory=dict)
    selection_artifact_id: str | None = None
    selection_version: str | None = None


class BuildKolDetailDraftTool(_BuilderToolBase):
    """把已抓取的达人详情 Evidence 转换为 kol_detail_v2 Draft。

    parent 权威绑定（§6.4，B3 与 ``CreateDraftTool`` 补齐一致）：Run 快照
    携带经归属校验的名单引用（``selection_version_id``）时，以快照为准
    覆盖 Draft 的 ``parent_artifact_id`` / ``parent_artifact_version_id``，
    不信任模型传参；模型传入的 selection 参数只进入 payload scope。
    """

    description = (
        "把达人详情 Evidence（identity/metrics/audience/trend/latest_posts 结构）"
        "确定性转换为达人详情 Draft（kol_detail_v2）。platform/kol_uid 为达人身份；"
        "evidence_id 为当前会话的详情证据；cache_state={hit, fetched_at, expires_at}"
        "为缓存元数据（首次抓取 hit=false，时间取抓取/过期时刻）。主页/原帖链接"
        "缺失会披露限制，不伪造链接。输出只含 "
        "artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        '{"platform":"xiaohongshu","kol_uid":"12345","evidence_id":"<详情证据 id>",'
        '"cache_state":{"hit":false,"fetched_at":"2026-08-01T10:00:00",'
        '"expires_at":"2026-08-02T10:00:00"}}（selection_artifact_id/selection_version '
        "为可选名单归属参数，无名单上下文时省略）。校验失败返回 draft_build_error "
        "字段级明细，按明细修正后重试。"
    )

    name = "build_kol_detail_draft"
    input_model = BuildKolDetailDraftArgs

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildKolDetailDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_kol_detail_draft requires a database session")
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error
        item = await self._load_evidence(context, args.evidence_id)
        if isinstance(item, ToolResult):
            return item
        detail, _base = unwrap_payload(item.raw_payload_json)
        if not isinstance(detail, dict):
            return _failed(
                DRAFT_BUILD_ERROR,
                f"evidence {args.evidence_id!r} does not contain a KOL detail object",
            )
        try:
            result = build_kol_detail_draft(
                platform=args.platform,
                kol_uid=args.kol_uid,
                detail=detail,
                evidence_id=item.id,
                cache_state=args.cache_state,
                selection_artifact_id=args.selection_artifact_id,
                selection_version=args.selection_version,
                source_names=(item.source_name,),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, str(exc))
        # §6.4 parent 权威绑定：Run 快照携带名单引用时覆盖 parent（不信任
        # 模型传参），与 CreateDraftTool 的 kol-detail 路径同一语义。
        snapshot_parent = await kol_detail_snapshot_selection_parent(self._db, context.run_id)
        if snapshot_parent[1] is not None:
            result = replace(
                result,
                parent_artifact_id=snapshot_parent[0],
                parent_artifact_version_id=snapshot_parent[1],
            )
        return await self._persist(context, result)


__all__ = [
    "BuildBrandReportDraftArgs",
    "BuildBrandReportDraftTool",
    "BuildCampaignReportDraftArgs",
    "BuildCampaignReportDraftTool",
    "BuildKolAnalysisDraftArgs",
    "BuildKolAnalysisDraftTool",
    "BuildKolDetailDraftArgs",
    "BuildKolDetailDraftTool",
    "BuildKolSelectionDraftArgs",
    "BuildKolSelectionDraftTool",
]
