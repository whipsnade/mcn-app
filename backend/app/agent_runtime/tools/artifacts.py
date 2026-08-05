"""Artifact Draft 工具（设计 §12.3 / Task 16）。

``create_draft`` / ``update_draft`` 通过 :class:`ArtifactService`（Task 12）
持久化模型产物（builder 输出）生成的强类型 Draft：零积分、``external_side_effect``
为 True、注册进 ToolRegistry 的 ``ARTIFACT_TOOLS`` 分类。工具只持久化，不决定
业务内容——payload / evidence_refs 由 builders 生成，Draft 身份由服务端
``build_artifact_key`` 稳定生成。

强类型直写护栏（§6.1，H2）：五类强类型正式 Artifact（brand_report_v3 /
campaign_report_v2 / kol_selection_v3 / kol_analysis_v2 / kol_detail_v2）
不允许经 ``create_draft`` / ``update_draft`` 直写 payload——创建与修订一律
走对应 ``build_*`` Builder（Builder 的 create_or_get 语义已覆盖「再构建即
追加新 Revision」）。直写在工具层以 ``typed_artifact_requires_builder``
结构化拒绝并回指 Builder；``insight_board_v1`` 不受限制。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact, ArtifactDraft
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadInvalid, SCHEMA_VERSION_BY_MODULE
from app.agent_runtime.kol_detail import KOL_DETAIL_SNAPSHOT_KEY
from app.agent_runtime.models import AgentRun
from app.agent_runtime.tools.contracts import ToolContext, ToolResult, truncate_summary

TYPED_ARTIFACT_REQUIRES_BUILDER = "typed_artifact_requires_builder"


def _payload_error_summary(exc: ArtifactPayloadInvalid) -> str:
    """artifact_payload_invalid → 字段级明细（loc: msg [type]），供模型自愈。

    仅报"N error(s)"时模型无法定位失败字段（真实 UAT 钻取场景 7 次盲改失败）。
    """
    parts: list[str] = []
    for error in exc.errors[:20]:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {error.get('msg')} [{error.get('type')}]")
    return truncate_summary(f"{exc}; " + "; ".join(parts) if parts else str(exc))

# 五类强类型正式 Artifact 的直写护栏：schema_version → 应使用的 Builder 工具名。
_TYPED_BUILDER_BY_SCHEMA: dict[str, str] = {
    "brand_report_v3": "build_brand_report_draft",
    "campaign_report_v2": "build_campaign_report_draft",
    "kol_selection_v3": "build_kol_selection_draft",
    "kol_analysis_v2": "build_kol_analysis_draft",
    "kol_detail_v2": "build_kol_detail_draft",
}


def _typed_builder_guard(schema_version: str) -> ToolResult | None:
    """schema_version 属于五类强类型时返回结构化拒绝（回指 Builder），否则 None。"""
    builder = _TYPED_BUILDER_BY_SCHEMA.get(schema_version)
    if builder is None:
        return None
    return ToolResult(
        status="failed",
        safe_summary=(
            f"{schema_version} 是强类型正式 Artifact，不允许用 create_draft / "
            f"update_draft 直写 payload；请改用 {builder}（提供 scope + evidence_id "
            "+ narrative，由 Builder 完成确定性聚合、字段级 lineage 与强类型校验；"
            f"修订同样重新调用 {builder}，会在同一 Artifact 上追加新 Revision）。"
        ),
        error_type=TYPED_ARTIFACT_REQUIRES_BUILDER,
    )


class CreateDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    business_fields: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any]
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    parent_artifact_id: str | None = None
    parent_artifact_version_id: str | None = None


async def kol_detail_snapshot_selection_parent(
    db: AsyncSession, run_id: str
) -> tuple[str | None, str | None]:
    """kol-detail Draft 的 parent 权威绑定（§6.4）。

    名单引用在 ``KolDetailRunService`` 归属校验后持久化到 Run 的
    ``prompt_snapshot_json``（含已发布名单 Version 行 id）。创建
    kol-detail Draft 时以快照为准覆盖模型传参——稳定行只记
    ``parent_artifact_id``，版本绑定写 Revision/Version 的
    ``parent_artifact_version_id``（沿用 ArtifactService 既有模式）。
    无名单引用时返回 ``(None, None)``，调用方保持模型原参数。

    由 ``BuildKolDetailDraftTool`` 消费（H2 起 ``create_draft`` 对 kol_detail_v2
    直写被 ``typed_artifact_requires_builder`` 护栏拒绝，Builder 是唯一落
    kol-detail Draft 的工具路径）。
    """
    run = await db.get(AgentRun, run_id)
    snapshot = run.prompt_snapshot_json if run is not None else None
    trigger = snapshot.get(KOL_DETAIL_SNAPSHOT_KEY) if isinstance(snapshot, dict) else None
    if not isinstance(trigger, dict):
        return None, None
    version_id = trigger.get("selection_version_id")
    if not version_id:
        return None, None
    return trigger.get("selection_artifact_id"), str(version_id)


class CreateDraftTool:
    """创建（或复用稳定身份后继续）一个 Artifact Draft。

    ``business_fields`` 只含业务字段；稳定 ``artifact_key`` 由服务端生成，模型
    不能直接指定数据库 key（设计 §8.1）。子 Artifact（如 kol_analysis_v2）通过
    ``parent_artifact_version_id`` 固定到当时的父 Version。

    强类型护栏（H2）：五类强类型正式 Artifact（brand/campaign/kol-selection/
    kol-analysis/kol-detail）直写一律拒绝并回指对应 Builder，本工具实际只服务
    ``insight_board_v1`` 的创建与受控修订。
    """

    description = (
        "持久化一个 Artifact Draft（正式产物，提交后经 Reviewer 审核发布）。"
        "限制：五类强类型正式 Artifact（brand_report_v3 / campaign_report_v2 / "
        "kol_selection_v3 / kol_analysis_v2 / kol_detail_v2）不允许用本工具直写，"
        "必须调用对应 build_* Builder 工具（brand→build_brand_report_draft、"
        "campaign→build_campaign_report_draft、kol-selection→build_kol_selection_draft、"
        "kol-analysis→build_kol_analysis_draft、kol-detail→build_kol_detail_draft），"
        "直写会被拒绝并回指 Builder；本工具仅用于 insight_board_v1 洞察看板。"
        "insight 的 business_fields=(parent_artifact_version_id, question)。"
        "artifact_type 与 schema_version 相同。payload 必须满足对应 schema；"
        "payload 中 data 下的每个业务数值都必须有一条 evidence_refs 条目（LineageRef："
        "artifact_path 为 RFC6901 JSON Pointer，sources 指向当前会话 evidence_id 的字段）。"
    )

    name = "create_draft"
    input_model = CreateDraftArgs
    points_cost = 0
    external_side_effect = True

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = CreateDraftArgs.model_validate(arguments)
        if self._db is None:
            return ToolResult(
                status="failed", safe_summary="create_draft requires a database session"
            )
        guard = _typed_builder_guard(args.schema_version)
        if guard is not None:
            return guard
        try:
            artifact, draft, revision = await ArtifactService(self._db).create_or_get_draft(
                session_id=context.session_id,
                user_id=context.user_id,
                run_id=context.run_id,
                module=args.module,
                business_fields=args.business_fields,
                schema_version=args.schema_version,
                payload=args.payload,
                evidence_refs=args.evidence_refs,
                artifact_type=args.artifact_type,
                parent_artifact_id=args.parent_artifact_id,
                parent_artifact_version_id=args.parent_artifact_version_id,
            )
        except ArtifactBusy as exc:
            return ToolResult(status="failed", safe_summary=str(exc), error_type=exc.code)
        except ArtifactPayloadInvalid as exc:
            return ToolResult(
                status="failed",
                safe_summary=_payload_error_summary(exc),
                error_type=exc.code,
            )
        return ToolResult(
            status="success",
            safe_summary=json.dumps(
                {
                    "artifact_id": artifact.id,
                    "artifact_key": artifact.artifact_key,
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "revision": revision.revision,
                    "status": draft.status,
                },
                ensure_ascii=False,
            ),
        )


class UpdateDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1)
    payload: dict[str, Any]
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class UpdateDraftTool:
    """乐观更新 Draft：追加不可变新 Revision，推进 current_revision。

    只有 working head 当前 owner（当前 Run）才能更新；他人抢占返回结构化
    ``artifact_busy``，不覆盖、不静默丢写。

    强类型护栏（H2）：目标 Draft 属于五类强类型正式 Artifact 时拒绝直写并
    回指对应 Builder（修订 = 重新调用 Builder 追加新 Revision）；仅
    ``insight_board_v1`` 允许本工具修订。
    """

    name = "update_draft"
    input_model = UpdateDraftArgs
    points_cost = 0
    external_side_effect = True

    description = (
        "乐观更新 Draft：追加不可变新 Revision。限制：五类强类型正式 Artifact "
        "（brand_report_v3 / campaign_report_v2 / kol_selection_v3 / "
        "kol_analysis_v2 / kol_detail_v2）的 Draft 不允许用本工具直写修订，"
        "修订需重新调用对应 build_* Builder（会在同一 Artifact 上追加新 Revision），"
        "直写会被拒绝并回指 Builder；本工具仅用于 insight_board_v1 的受控修订。"
    )

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = UpdateDraftArgs.model_validate(arguments)
        if self._db is None:
            return ToolResult(
                status="failed", safe_summary="update_draft requires a database session"
            )
        # 护栏按目标 Draft 的稳定身份 module 判定（与 payload 内容无关）；
        # Draft 不存在时放行给 ArtifactService 走既有 KeyError 语义。
        draft_row = await self._db.get(ArtifactDraft, args.draft_id)
        if draft_row is not None:
            artifact_row = await self._db.get(AgentArtifact, draft_row.artifact_id)
            if artifact_row is not None:
                guard = _typed_builder_guard(
                    SCHEMA_VERSION_BY_MODULE.get(artifact_row.module, "")
                )
                if guard is not None:
                    return guard
        try:
            draft, revision = await ArtifactService(self._db).update_draft(
                run_id=context.run_id,
                draft_id=args.draft_id,
                payload=args.payload,
                evidence_refs=args.evidence_refs,
            )
        except KeyError as exc:
            return ToolResult(status="failed", safe_summary=str(exc))
        except ArtifactBusy as exc:
            return ToolResult(status="failed", safe_summary=str(exc), error_type=exc.code)
        except ArtifactPayloadInvalid as exc:
            return ToolResult(
                status="failed",
                safe_summary=_payload_error_summary(exc),
                error_type=exc.code,
            )
        return ToolResult(
            status="success",
            safe_summary=json.dumps(
                {
                    "artifact_id": draft.artifact_id,
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "revision": revision.revision,
                    "current_revision": draft.current_revision,
                    "status": draft.status,
                },
                ensure_ascii=False,
            ),
        )


__all__ = [
    "CreateDraftArgs",
    "CreateDraftTool",
    "UpdateDraftArgs",
    "UpdateDraftTool",
    "kol_detail_snapshot_selection_parent",
]
