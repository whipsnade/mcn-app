"""Artifact Builder 工具（v3 加固 §3.3/§6.1，B2；H5 增 insight）。

六个 Builder 工具把「模型手写带 lineage 的大型正式 payload」替换为确定性
转换：模型只提供用户确认的 scope、Evidence ID（按章节分组）与叙事字段；
工具按 ID 读取当前 Session 的 Evidence（归属校验，跨 Session 一律
``evidence_not_found`` 不泄漏存在性）、调用 ``agent_artifacts.builders``
的确定性 Builder、经 :class:`ArtifactService` 落 Draft。insight 钻取看板
（H5）更进一步：模型连数值都不填，只给板块结构与每个数字的 value_ref
引用，工具解析引用并复制真实值后由 Builder 组装。

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
from app.agent_artifacts.builders.insight import (
    BarChartBlockSpec,
    BlockSpec,
    CalculationValueRef,
    EvidenceValueRef,
    LineChartBlockSpec,
    MarkdownBlockSpec,
    MetricGridBlockSpec,
    PieChartBlockSpec,
    ReferencesBlockSpec,
    ResolvedBlock,
    ResolvedLineage,
    TableBlockSpec,
    TableCellRef,
    TimelineBlockSpec,
    ValueRef,
    build_insight_draft,
)
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft
from app.agent_artifacts.builders.raw_rows import extract_rows, unwrap_payload
from app.agent_artifacts.lineage import PointerError, resolve_pointer
from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_artifacts.payloads.kol_analysis import KolAnalysisNarrative
from app.agent_artifacts.payloads.kol_selection import KolSelectionNarrative
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadInvalid
from app.agent_runtime.models import (
    AgentRun,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.tools.artifacts import kol_detail_snapshot_selection_parent
from app.agent_runtime.tools.contracts import (
    ToolContext,
    ToolResult,
    format_validation_error,
    truncate_summary,
)
from app.db.session import SessionFactory
from app.pi_gateway.loop_guard import LoopGuard

NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
EVIDENCE_NOT_FOUND = "evidence_not_found"
DRAFT_BUILD_ERROR = "draft_build_error"

# 结构化错误回喂的长度上限（与 contracts.ERROR_SUMMARY_LIMIT 同源）。
_ERROR_SUMMARY_LIMIT = 2000


def _failed(error_type: str, message: str) -> ToolResult:
    return ToolResult(status="failed", safe_summary=message, error_type=error_type)


def _truncate(text: str, limit: int = _ERROR_SUMMARY_LIMIT) -> str:
    return truncate_summary(text, limit)


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic 校验失败 → 字段级明细（``loc: msg [type]``），截断到上限。"""
    return format_validation_error(exc, prefix="invalid builder arguments: ")


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

    def __init__(
        self,
        db_session: AsyncSession | None = None,
        *,
        durable_session_factory: Any = SessionFactory,
    ) -> None:
        self._db = db_session
        # The current-executor path uses an independent transaction so a
        # crashed long-running worker cannot lose guard progress. Pi Gateway
        # internal-tool requests already hold the Run lock in this session;
        # they pass None to avoid waiting on their own uncommitted lock.
        self._durable_session_factory = durable_session_factory

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        guard: LoopGuard | None = None
        run: AgentRun | None = None
        if self._db is not None:
            run = await self._db.get(AgentRun, context.run_id)
            if run is not None:
                guard = LoopGuard(
                    self._db,
                    durable_session_factory=self._durable_session_factory,
                )
                blocked = await guard.reject_if_open(run)
                if blocked is not None:
                    return blocked
        try:
            result = await self._execute(context, arguments)
        except ValidationError as exc:
            result = _failed(DRAFT_BUILD_ERROR, _format_validation_error(exc))
        except DraftBuildError as exc:
            result = _failed(DRAFT_BUILD_ERROR, _truncate(str(exc)))
        if guard is not None and run is not None:
            return await guard.record_builder_result(run, self.name, result)
        return result

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
        "点分路径（data. 前缀可省略）。数字纪律：narrative 中的每个数字都必须能在 "
        "data 的 supporting_paths 指向的位置找到同值，找不到就不要写这个数字。"
        "正确：data.overview.total_volume=295614 时 findings 写「本期总声量 295614」"
        "并以 supporting_paths 指向它；错误：data.comparisons.mom.metrics 全为 null "
        "时在 narrative 写「环比增长 54.9%」——对比数据缺失就不得给出任何涨跌幅数字。"
        "参数或 scope 校验失败返回 draft_build_error "
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
        "scope={brand, campaign, period{start,end,timezone}, platforms[], keywords[], "
        "exclusions[], official_accounts[], comparison_mode, attribution_rules[], "
        "upload_ids[]}；"
        "evidence 分组（键："
        + "/".join(CAMPAIGN_EVIDENCE_GROUPS)
        + "）：posts 为原始帖行（必需章节主数据源），sentiment 为可选情感明细行"
        "（缺失时用 posts 行情感字段）；current/baseline/post 为活动期/活动前/"
        "活动后观察期行（周期对比）；social 为社媒补充行（与 posts 同口径，"
        "DataTap 为主）；upload 为用户补充资料行（成本/转化/内部指标优先取用）。"
        "narrative 可选（executive_summary/"
        "phase_review[]/findings[]/recommendations[]，supporting_paths 必须指向"
        " data 内路径）。输出只含 artifact_id/draft_id/revision_id/schema_version"
        " 与受限摘要。"
        "输入契约示例："
        'scope={"brand":"瑞幸咖啡","campaign":"生椰拿铁上新","period":{"start":"2026-07-01",'
        '"end":"2026-07-31","timezone":"Asia/Shanghai"},"platforms":["xiaohongshu"],'
        '"keywords":["生椰拿铁"],"attribution_rules":["最后点击 7 天"]}；'
        'evidence={"posts":["ev-1"],"sentiment":["ev-2"],"baseline":["ev-3"],'
        '"upload":["ev-4"]}；'
        'narrative={"executive_summary":"...","phase_review":[{"phase":"预热期","detail":"...",'
        '"supporting_paths":["data.daily_trend"]}],"findings":[{"title":"...","detail":"...",'
        '"supporting_paths":["data.overview.total_engagement"]}],"recommendations":[{"title":"...",'
        '"action":"...","rationale":"...","supporting_paths":["data.top_posts"]}]}。'
        "注意：phase_review 条目为 {phase, detail, supporting_paths}；findings 条目字段是 "
        "title/detail（不是 description）；recommendations 条目必须含 title/action/rationale；"
        "supporting_paths 是 data 下真实存在的点分路径。数字纪律：narrative 中的每个数字"
        "都必须能在 data 的 supporting_paths 指向的位置找到同值，找不到就不要写这个数字。"
        "正确：data.overview.total_engagement=365 时 findings 写「合计互动 365」并以 "
        "supporting_paths 指向它；错误：data.overview.total_engagement 为 null 时在 "
        "narrative 写「互动量破百万」——数据缺失就不得给出任何具体数字。"
        "校验失败返回 draft_build_error "
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
    # 模型叙事（设计 §6.1：Builder 输入必须包含模型提供的叙事字段）。嵌套模型
    # 直接复用 kol_selection_v3 payload 的 KolSelectionNarrative，契约同源不漂移；
    # 可空——缺省时由 builder 按评分结果确定性生成兜底叙事。
    narrative: KolSelectionNarrative | None = None


class BuildKolSelectionDraftTool(_BuilderToolBase):
    """构建 KOL 圈选名单 Draft（kol_selection_v3；评分委托 rank_kols/kol_value_score_v3）。"""

    description = (
        "把 KOL 列表 Evidence 确定性转换为圈选名单 Draft（kol_selection_v3）。"
        "scope 为圈选条件对象（brand/category/campaign 可空、platforms[]、"
        "audience{regions[],age_ranges[],interests[]}、filters 预算/粉丝门槛、"
        "content_formats[] 用户确认的内容形式——报价须匹配其一才计有效）；"
        "evidence_id 为当前会话的 KOL 列表证据（列表行含 platform/kol_uid/"
        "nickname/followers/engagement_total/score_inputs 等；MCP 原始中文行"
        "如 平台/账号ID (kwUid)/昵称/粉丝数/平均互动 会自动归一，无需手工改写）。"
        "评分由确定性 "
        "rank_kols（kol_value_score_v3：效果与匹配度 70 + 价格效率 30）完成，"
        "默认跨平台 Top20 按价值总分降序。"
        "narrative 可选（selection_summary 必填 + fit_findings[]/risk_notes[]/"
        "usage_advice[]，条目为 {text, kol_uid 可空, supporting_paths[]}；"
        "supporting_paths 必须指向 data 内真实存在的点分路径，如 "
        "data.items.0.score_snapshot.value_score；缺省时由工具按评分结果确定性生成）。"
        "输出只含 artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        'scope={"brand":null,"category":"美食","campaign":null,"platforms":["小红书"],'
        '"audience":{"regions":["上海"],"age_ranges":["18-24"],"interests":["美食"]},'
        '"filters":{"budget_min":null,"budget_max":100000,"follower_min":10000,'
        '"follower_max":null},"content_formats":["视频","图文"]}；'
        'evidence_id="<当前会话 KOL 列表证据 id>"；'
        'narrative={"selection_summary":"...","fit_findings":[{"text":"...",'
        '"kol_uid":"123","supporting_paths":["data.items.0.score_snapshot.value_score"]}],'
        '"risk_notes":[],"usage_advice":[{"text":"...","supporting_paths":'
        '["data.items.0.engagement_total"]}]}。'
        "注意：audience 与 filters 必填；filters 只有 budget_min/budget_max/"
        "follower_min/follower_max 四个字段（不存在 follower_threshold 等其他字段）。"
        "数字纪律：narrative 中的每个数字都必须能在 supporting_paths 指向的 data "
        "位置找到同值，找不到就不要写这个数字。多传或错传字段返回 draft_build_error "
        "字段级明细，按明细修正后重试。"
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
        row_refs = extract_rows(item.id, item.raw_payload_json)
        if not row_refs:
            return _failed(
                DRAFT_BUILD_ERROR,
                f"evidence {args.evidence_id!r} does not contain a KOL item list",
            )
        try:
            result = await build_kol_selection_draft(
                scope=args.scope,
                evidence_id=item.id,
                items=[ref.row for ref in row_refs],
                context=context,
                db=self._db,
                source_names=(item.source_name,),
                narrative=(
                    args.narrative.model_dump(mode="json")
                    if args.narrative is not None
                    else None
                ),
                # 行的完整基准路径（含容器键前缀）——lineage source_path 必须在
                # Evidence raw payload 内可解析（H1：中文容器键缺前缀导致
                # evidence_source_path_not_found）。
                row_source_paths=[ref.field_base for ref in row_refs],
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
    # 模型叙事（设计 §6.1；H4——Reviewer 可要求逐人分析，确定性组合级模板叙事
    # 无法满足，Args 不收 narrative 会造成契约死锁）。嵌套模型直接复用
    # kol_analysis_v2 payload 的 KolAnalysisNarrative，契约同源不漂移；可空——
    # 缺省时由 builder 按名单数据确定性生成兜底叙事。
    narrative: KolAnalysisNarrative | None = None


class BuildKolAnalysisDraftTool(_BuilderToolBase):
    """对已发布圈选名单 Version 构建 KOL 分析 Draft（kol_analysis_v2）。"""

    description = (
        "对已发布的圈选名单（kol_selection_v3 Version）做组合分析，产出 "
        "kol_analysis_v2 Draft。selection_artifact_id 为当前会话的名单 "
        "Artifact id；selection_version 缺省取最新已发布版本。分析数据引用"
        "名单 Version 并递归追溯其 Evidence。narrative 可选（executive_summary "
        "必填 + portfolio_findings[]/mix_recommendations[]/risk_notes[]，条目为 "
        "{title, detail, supporting_paths[]}；supporting_paths 必须指向 data 内"
        "真实存在的点分路径，如 data.top_kols.0.score、data.summary.avg_score；"
        "缺省时由工具按名单数据确定性生成组合级兜底叙事）。Reviewer 会核对 "
        "narrative 与 scope.selection_version 对应名单的 top_kols 覆盖度——"
        "Reviewer 要求逐人分析时，在 portfolio_findings 中为头部达人逐一给出"
        "核心价值条目并以 supporting_paths 指向该达人 data.top_kols 下的真实"
        "字段。输出只含 "
        "artifact_id/draft_id/revision_id/schema_version 与受限摘要。"
        "输入契约示例："
        '{"selection_artifact_id":"<名单 Artifact id>","selection_version":1,'
        '"analysis_period":"2026-07","narrative":{"executive_summary":"...",'
        '"portfolio_findings":[{"title":"达人A 核心价值","detail":"...",'
        '"supporting_paths":["data.top_kols.0.score"]}],"mix_recommendations":[],'
        '"risk_notes":[]}}（selection_version/analysis_period/narrative 均可省略）。'
        "数字纪律：narrative 中的每个数字都必须能在 supporting_paths 指向的 data "
        "位置找到同值，找不到就不要写这个数字。名单未发布或 Artifact 不属于当前"
        "会话返回 not_found；其他校验失败返回 "
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
                narrative=(
                    args.narrative.model_dump(mode="json")
                    if args.narrative is not None
                    else None
                ),
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
        detail, base = unwrap_payload(item.raw_payload_json)
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
                # {"result": "<json>"} 包装时基路径为 /result，lineage 粗粒度
                # 指向整个串（指针无法下钻字符串，但必须可解析）。
                source_base=base,
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


# ---------------------------------------------------------------------------
# build_insight_draft（H5：开放式钻取看板收口）
# ---------------------------------------------------------------------------


class BuildInsightDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_artifact_version_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scope: dict[str, Any] = Field(default_factory=dict)
    blocks: list[BlockSpec] = Field(min_length=1, max_length=50)
    narrative: dict[str, Any] | None = None


# 父 Artifact module → payload module（ArtifactPayloadBase 三值字面量）。
_PAYLOAD_MODULE_BY_PARENT = {"brand": "brand", "campaign": "campaign"}


class BuildInsightDraftTool(_BuilderToolBase):
    """对已发布父 Version 构建钻取看板 Draft（insight_board_v1）。

    与其他 Builder 的差别：模型连数值都不填——板块规格里每个数字都是
    ``value_ref`` 引用（evidence / artifact / calculation 三来源），本工具
    解析引用、复制真实值并做归属校验（跨 Session 一律拒绝），再由
    ``builders.insight`` 确定性组装 payload 与数字级 lineage。
    """

    name = "build_insight_draft"
    input_model = BuildInsightDraftArgs

    description = (
        "对已发布的父 Artifact Version 做一次开放式钻取，产出 insight_board_v1 "
        "看板 Draft。你决定钻取问题、数据和图表结构；Builder 只做确定性组装"
        "（取值、lineage、强类型校验）。parent_artifact_version_id 为当前会话"
        "已发布 Version 的 id（brand/campaign/kol_selection/kol_analysis/"
        "kol_detail/insight 均可作父级，可用 read_artifact 查到）；question 为"
        "用户本轮钻取问题；title/scope 为看板标题与范围（scope 字段仅限 "
        "summary/period{start,end,timezone}/platforms[]/brand/campaign/kol_uid）。"
        "blocks 为板块规格数组（1-50 块，判别字段 type，共 8 种）：\n"
        '1) {"type":"metric_grid","title":"...","cards":[{"key":"total_volume",'
        '"label":"声量","value_ref":{...},"unit":"条"}]}（cards≤16，path 可选）；\n'
        '2) {"type":"table","title":"...","columns":["平台","声量"],"rows":[["小红书",'
        '{"value_ref":{...}}]]}（文本单元格直接给字符串，数值单元格必须是 '
        '{"value_ref":{...}}，columns 不重复）；\n'
        '3) {"type":"bar_chart","title":"...","categories":["小红书"],"series":'
        '[{"name":"声量","values":[{...value_ref...}]}]}；\n'
        '4) {"type":"line_chart","title":"...","x_labels":["2026-07-01"],"series":'
        '[{"name":"声量","values":[{...value_ref...}]}]}；\n'
        '5) {"type":"pie_chart","title":"...","slices":[{"name":"正面","value_ref":{...}}]}；\n'
        '6) {"type":"markdown","title":"...","content":"...markdown 文本..."}；\n'
        '7) {"type":"timeline","title":"...","items":[{"date":"2026-07-01","title":"上新",'
        '"description":"可选"}]}；\n'
        '8) {"type":"references","title":"...","items":[{"label":"原帖",'
        '"url":"https://..."}]}。\n'
        "数字纪律（硬性）：metric 值、series 数值、pie 值、table 数字单元格一律"
        "用 value_ref 引用，不允许直接填写数值字面值；裸数字会被拒绝。value_ref "
        "三选一：\n"
        '- {"source_type":"evidence","evidence_id":"<本会话证据 id>",'
        '"source_path":"/行/字段"}（RFC6901 指向证据 raw payload 内的字段）；\n'
        '- {"source_type":"artifact","artifact_version_id":"<本会话已发布 Version id>",'
        '"source_path":"/data/..."}（指向该 Version payload 内的字段）；\n'
        '- {"source_type":"calculation","tool_call_id":"<已 settled 计算调用 id>",'
        '"result_path":"/value","input_refs":[{"source_type":"evidence","evidence_id":"...",'
        '"source_path":"/..."}]}（指向 calculate_expression 等内部计算工具的结果字段；'
        "input_refs≥1，是该计算的输入来源，作为 lineage 基座）。\n"
        "source_path/result_path 必须真实可解析，evidence/artifact/计算调用都必须属于"
        "当前会话；引用失败返回 draft_build_error 并指明出错的板块位置（如 "
        "blocks.0.cards.0），按明细修正后重试。narrative 可选（{summary, findings[]"
        "{title,detail,supporting_paths[]}}，supporting_paths 指向 data 内真实路径，"
        "如 data.0.cards.0.value；缺省时工具按 question 生成兜底叙事）。输出只含 "
        "artifact_id/draft_id/revision_id/schema_version 与受限摘要；同一父 Version "
        "上的同一 question 复用同一 Artifact，Reviewer 打回后重调本工具追加新 Revision。"
    )

    async def _execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = BuildInsightDraftArgs.model_validate(arguments)
        if self._db is None:
            return _failed(DRAFT_BUILD_ERROR, "build_insight_draft requires a database session")
        session_error = await self._check_session(context)
        if session_error is not None:
            return session_error

        # 父 Version 归属校验：Version 行只在发布时产生（存在即已发布）；
        # 不存在或跨 Session 一律 not_found，不泄漏存在性。
        row = (
            await self._db.execute(
                select(AgentArtifactVersion, AgentArtifact)
                .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                .where(AgentArtifactVersion.id == args.parent_artifact_version_id)
            )
        ).first()
        if row is None:
            return _failed(NOT_FOUND, "parent_artifact_version_not_found")
        parent_version, parent_artifact = row
        if parent_artifact.session_id != context.session_id:
            return _failed(NOT_FOUND, "parent_artifact_version_not_found")

        errors: list[str] = []
        resolved_blocks: list[ResolvedBlock] = []
        for index, spec in enumerate(args.blocks):
            resolved_blocks.append(await self._resolve_block(context, index, spec, errors))
        if errors:
            return _failed(DRAFT_BUILD_ERROR, _truncate("; ".join(errors)))

        try:
            result = build_insight_draft(
                question=args.question,
                title=args.title,
                module=_PAYLOAD_MODULE_BY_PARENT.get(parent_artifact.module, "kol"),
                scope=args.scope,
                parent_artifact_id=parent_artifact.id,
                parent_artifact_version_id=parent_version.id,
                blocks=resolved_blocks,
                narrative=args.narrative,
                source_names=("insight_evidence",),
            )
        except DraftBuildError as exc:
            return _failed(DRAFT_BUILD_ERROR, _truncate(str(exc)))
        return await self._persist(context, result)

    # ------------------------------------------------------------------ 取值

    async def _resolve_block(
        self, context: ToolContext, index: int, spec: BlockSpec, errors: list[str]
    ) -> ResolvedBlock:
        """把板块规格解析为「已取值的 payload block + 数字级 lineage」。"""
        where = f"blocks.{index}"
        lineage: list[ResolvedLineage] = []

        if isinstance(spec, MetricGridBlockSpec):
            cards: list[dict[str, Any]] = []
            for j, card in enumerate(spec.cards):
                resolved = await self._resolve_ref(
                    context, card.value_ref, f"{where}.cards.{j}.value_ref", False, errors
                )
                if resolved is None:
                    continue
                value, sources, derivation = resolved
                entry: dict[str, Any] = {"key": card.key, "label": card.label, "value": value}
                if card.unit is not None:
                    entry["unit"] = card.unit
                if card.path is not None:
                    entry["path"] = card.path
                cards.append(entry)
                lineage.append(
                    ResolvedLineage(f"/data/{index}/cards/{j}/value", sources, derivation)
                )
            block = {"block_type": "metric_grid", "title": spec.title, "cards": cards}

        elif isinstance(spec, TableBlockSpec):
            rows: list[list[Any]] = []
            for r, row_cells in enumerate(spec.rows):
                out_row: list[Any] = []
                for c, cell in enumerate(row_cells):
                    if isinstance(cell, str):
                        out_row.append(cell)
                        continue
                    assert isinstance(cell, TableCellRef)
                    resolved = await self._resolve_ref(
                        context, cell.value_ref, f"{where}.rows.{r}.{c}", False, errors
                    )
                    if resolved is None:
                        continue
                    value, sources, derivation = resolved
                    out_row.append(value)
                    lineage.append(
                        ResolvedLineage(f"/data/{index}/rows/{r}/{c}", sources, derivation)
                    )
                rows.append(out_row)
            block = {
                "block_type": "table",
                "title": spec.title,
                "columns": list(spec.columns),
                "rows": rows,
            }

        elif isinstance(spec, (BarChartBlockSpec, LineChartBlockSpec)):
            series: list[dict[str, Any]] = []
            for s, series_spec in enumerate(spec.series):
                values: list[Any] = []
                for v, ref in enumerate(series_spec.values):
                    resolved = await self._resolve_ref(
                        context, ref, f"{where}.series.{s}.values.{v}", True, errors
                    )
                    if resolved is None:
                        continue
                    value, sources, derivation = resolved
                    values.append(value)
                    lineage.append(
                        ResolvedLineage(
                            f"/data/{index}/series/{s}/values/{v}", sources, derivation
                        )
                    )
                series.append({"name": series_spec.name, "values": values})
            if isinstance(spec, BarChartBlockSpec):
                block = {
                    "block_type": "bar_chart",
                    "title": spec.title,
                    "categories": list(spec.categories),
                    "series": series,
                }
            else:
                block = {
                    "block_type": "line_chart",
                    "title": spec.title,
                    "x_labels": list(spec.x_labels),
                    "series": series,
                }

        elif isinstance(spec, PieChartBlockSpec):
            slices: list[dict[str, Any]] = []
            for s, slice_spec in enumerate(spec.slices):
                resolved = await self._resolve_ref(
                    context, slice_spec.value_ref, f"{where}.slices.{s}.value_ref", True, errors
                )
                if resolved is None:
                    continue
                value, sources, derivation = resolved
                slices.append({"name": slice_spec.name, "value": value})
                lineage.append(
                    ResolvedLineage(f"/data/{index}/slices/{s}/value", sources, derivation)
                )
            block = {"block_type": "pie_chart", "title": spec.title, "slices": slices}

        elif isinstance(spec, MarkdownBlockSpec):
            block = {"block_type": "markdown", "title": spec.title, "content": spec.content}

        elif isinstance(spec, TimelineBlockSpec):
            block = {
                "block_type": "timeline",
                "title": spec.title,
                "items": [
                    {
                        "date": item.date.isoformat(),
                        "title": item.title,
                        "description": item.description,
                    }
                    for item in spec.items
                ],
            }

        elif isinstance(spec, ReferencesBlockSpec):
            block = {
                "block_type": "references",
                "title": spec.title,
                "items": [{"label": item.label, "url": item.url} for item in spec.items],
            }

        else:  # pragma: no cover - 判别联合已穷尽 8 型
            raise DraftBuildError(f"unsupported block spec: {type(spec).__name__}")

        return ResolvedBlock(block=block, lineage=lineage)

    async def _resolve_ref(
        self,
        context: ToolContext,
        ref: ValueRef,
        where: str,
        expect_numeric: bool,
        errors: list[str],
    ) -> tuple[Any, list[dict[str, Any]], dict[str, Any] | None] | None:
        """解析一个 value_ref：复制真实值并产出 lineage 来源/推导。

        失败时向 ``errors`` 追加带板块位置的明细并返回 None（继续解析其余
        引用，一次性回喂全部错误）。
        """
        if isinstance(ref, EvidenceValueRef):
            resolved = await self._resolve_evidence_source(
                context, ref.evidence_id, ref.source_path, where, errors
            )
            if resolved is None:
                return None
            value, source = resolved
            sources = [source]
            derivation = None
        elif isinstance(ref, CalculationValueRef):
            outcome = await self._resolve_calculation(context, ref, where, errors)
            if outcome is None:
                return None
            value, sources, derivation = outcome
        else:  # ArtifactValueRef
            resolved = await self._resolve_artifact_source(
                context, ref.artifact_version_id, ref.source_path, where, errors
            )
            if resolved is None:
                return None
            value, source = resolved
            sources = [source]
            derivation = None

        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors.append(
                f"{where}: resolved value must be a scalar (string/number), "
                f"got {type(value).__name__}"
            )
            return None
        if expect_numeric and not isinstance(value, (int, float)):
            errors.append(
                f"{where}: chart values must resolve to a number, got {value!r}"
            )
            return None
        return value, sources, derivation

    async def _resolve_evidence_source(
        self, context: ToolContext, evidence_id: str, source_path: str, where: str, errors: list[str]
    ) -> tuple[Any, dict[str, Any]] | None:
        item = await self._load_evidence(context, evidence_id)
        if isinstance(item, ToolResult):
            errors.append(f"{where}: {item.safe_summary}")
            return None
        try:
            value = resolve_pointer(item.raw_payload_json, source_path)
        except PointerError as exc:
            errors.append(f"{where}: {exc}")
            return None
        return value, {
            "source_type": "evidence",
            "evidence_id": item.id,
            "source_path": source_path,
        }

    async def _resolve_artifact_source(
        self,
        context: ToolContext,
        artifact_version_id: str,
        source_path: str,
        where: str,
        errors: list[str],
    ) -> tuple[Any, dict[str, Any]] | None:
        row = (
            await self._db.execute(
                select(AgentArtifactVersion, AgentArtifact.session_id)
                .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                .where(AgentArtifactVersion.id == artifact_version_id)
            )
        ).first()
        if row is None:
            errors.append(
                f"{where}: artifact version not found in current session: "
                f"{artifact_version_id!r}"
            )
            return None
        version, session_id = row
        if session_id != context.session_id:
            errors.append(
                f"{where}: artifact version not found in current session: "
                f"{artifact_version_id!r}"
            )
            return None
        try:
            value = resolve_pointer(version.payload_json, source_path)
        except PointerError as exc:
            errors.append(f"{where}: {exc}")
            return None
        return value, {
            "source_type": "artifact",
            "artifact_version_id": artifact_version_id,
            "source_path": source_path,
        }

    async def _resolve_calculation(
        self, context: ToolContext, ref: CalculationValueRef, where: str, errors: list[str]
    ) -> tuple[Any, list[dict[str, Any]], dict[str, Any]] | None:
        """解析计算来源：已 settled 内部计算调用结果字段 + input_refs 基座。"""
        row = (
            await self._db.execute(
                select(AgentToolCall, AgentRun.session_id, AgentStep.output_json)
                .join(AgentRun, AgentRun.id == AgentToolCall.run_id)
                .join(AgentStep, AgentStep.id == AgentToolCall.step_id)
                .where(AgentToolCall.id == ref.tool_call_id)
            )
        ).first()
        if row is None:
            errors.append(
                f"{where}: calculation tool call not found in current session: "
                f"{ref.tool_call_id!r}"
            )
            return None
        call, session_id, output_json = row
        if session_id != context.session_id:
            errors.append(
                f"{where}: calculation tool call not found in current session: "
                f"{ref.tool_call_id!r}"
            )
            return None
        if call.status != "settled":
            errors.append(
                f"{where}: calculation tool call {ref.tool_call_id!r} status "
                f"{call.status!r} is not settled"
            )
            return None
        if call.service != "internal":
            errors.append(
                f"{where}: calculation tool call {ref.tool_call_id!r} service "
                f"{call.service!r} is not internal"
            )
            return None
        summary = output_json.get("safe_summary") if isinstance(output_json, dict) else None
        try:
            result_payload = json.loads(summary) if isinstance(summary, str) else None
        except json.JSONDecodeError:
            result_payload = None
        if result_payload is None:
            errors.append(
                f"{where}: calculation tool call {ref.tool_call_id!r} result payload "
                "unavailable"
            )
            return None
        try:
            value = resolve_pointer(result_payload, ref.result_path)
        except PointerError as exc:
            errors.append(f"{where}: {exc}")
            return None

        # input_refs 是 derivation 的输入基座：必须同属当前 Session 且可解析。
        sources: list[dict[str, Any]] = []
        for input_ref in ref.input_refs:
            if isinstance(input_ref, EvidenceValueRef):
                resolved = await self._resolve_evidence_source(
                    context, input_ref.evidence_id, input_ref.source_path, where, errors
                )
            else:
                resolved = await self._resolve_artifact_source(
                    context,
                    input_ref.artifact_version_id,
                    input_ref.source_path,
                    where,
                    errors,
                )
            if resolved is None:
                return None
            sources.append(resolved[1])
        derivation = {
            "tool_call_id": call.id,
            "method": call.internal_tool_name,
            "input_paths": [input_ref.source_path for input_ref in ref.input_refs],
        }
        return value, sources, derivation


__all__ = [
    "BuildBrandReportDraftArgs",
    "BuildBrandReportDraftTool",
    "BuildCampaignReportDraftArgs",
    "BuildCampaignReportDraftTool",
    "BuildInsightDraftArgs",
    "BuildInsightDraftTool",
    "BuildKolAnalysisDraftArgs",
    "BuildKolAnalysisDraftTool",
    "BuildKolDetailDraftArgs",
    "BuildKolDetailDraftTool",
    "BuildKolSelectionDraftArgs",
    "BuildKolSelectionDraftTool",
]
