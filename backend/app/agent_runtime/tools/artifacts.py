"""Artifact Draft 工具（设计 §12.3 / Task 16）。

``create_draft`` / ``update_draft`` 通过 :class:`ArtifactService`（Task 12）
持久化模型产物（builder 输出）生成的强类型 Draft：零积分、``external_side_effect``
为 True、注册进 ToolRegistry 的 ``ARTIFACT_TOOLS`` 分类。工具只持久化，不决定
业务内容——payload / evidence_refs 由 builders 生成，Draft 身份由服务端
``build_artifact_key`` 稳定生成。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadInvalid, SCHEMA_VERSION_BY_MODULE
from app.agent_runtime.kol_detail import KOL_DETAIL_SNAPSHOT_KEY
from app.agent_runtime.models import AgentRun
from app.agent_runtime.tools.contracts import ToolContext, ToolResult


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


_ARTIFACT_FIELD_HINTS: dict[str, dict[str, str]] = {
    "brand": {"brand": "品牌名"},
    "campaign": {"brand": "品牌名", "campaign": "活动名"},
    "kol-selection": {"scope": "圈选条件对象（品牌/平台/粉丝要求等）"},
    "kol-analysis": {"selection_artifact_id": "父圈选名单 Artifact id"},
    "kol-detail": {"platform": "平台", "kol_uid": "达人 uid"},
    "insight": {"parent_artifact_version_id": "父已发布 Version id", "question": "钻取问题"},
}

# module → (schema_version, business_fields 提示)；schema_version 与发布校验边界的
# 固定组合（agent_artifacts.validation.SCHEMA_VERSION_BY_MODULE）保持同一真源。
_ARTIFACT_MODULES = {
    module: (SCHEMA_VERSION_BY_MODULE[module], fields)
    for module, fields in _ARTIFACT_FIELD_HINTS.items()
}


class CreateDraftTool:
    """创建（或复用稳定身份后继续）一个 Artifact Draft。

    ``business_fields`` 只含业务字段；稳定 ``artifact_key`` 由服务端生成，模型
    不能直接指定数据库 key（设计 §8.1）。子 Artifact（如 kol_analysis_v2）通过
    ``parent_artifact_version_id`` 固定到当时的父 Version。
    """

    description = (
        "持久化一个强类型 Artifact Draft（正式产物，提交后经 Reviewer 审核发布）。"
        "可用 module → (schema_version, business_fields) 对照："
        + ", ".join(
            f"{module}:{schema}(business_fields={list(fields)})"
            for module, (schema, fields) in _ARTIFACT_MODULES.items()
        )
        + "。artifact_type 与 schema_version 相同。payload 必须满足对应 schema；"
        "payload 中 data 下的每个业务数值都必须有一条 evidence_refs 条目（LineageRef："
        "artifact_path 为 RFC6901 JSON Pointer，sources 指向当前会话 evidence_id 的字段）。"
    )

    name = "create_draft"
    input_model = CreateDraftArgs
    points_cost = 0
    external_side_effect = True

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def _kol_detail_selection_parent(
        self, context: ToolContext
    ) -> tuple[str | None, str | None]:
        """kol-detail Draft 的 parent 权威绑定（§6.4）。

        名单引用在 ``KolDetailRunService`` 归属校验后持久化到 Run 的
        ``prompt_snapshot_json``（含已发布名单 Version 行 id）。创建
        kol-detail Draft 时以快照为准覆盖模型传参——稳定行只记
        ``parent_artifact_id``，版本绑定写 Revision/Version 的
        ``parent_artifact_version_id``（沿用 ArtifactService 既有模式）。
        无名单引用时返回 ``(None, None)``，调用方保持模型原参数。
        """
        run = await self._db.get(AgentRun, context.run_id)
        snapshot = run.prompt_snapshot_json if run is not None else None
        trigger = (
            snapshot.get(KOL_DETAIL_SNAPSHOT_KEY) if isinstance(snapshot, dict) else None
        )
        if not isinstance(trigger, dict):
            return None, None
        version_id = trigger.get("selection_version_id")
        if not version_id:
            return None, None
        return trigger.get("selection_artifact_id"), str(version_id)

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = CreateDraftArgs.model_validate(arguments)
        if self._db is None:
            return ToolResult(
                status="failed", safe_summary="create_draft requires a database session"
            )
        parent_artifact_id = args.parent_artifact_id
        parent_artifact_version_id = args.parent_artifact_version_id
        if args.module == "kol-detail":
            snapshot_parent = await self._kol_detail_selection_parent(context)
            if snapshot_parent[1] is not None:
                parent_artifact_id, parent_artifact_version_id = snapshot_parent
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
                parent_artifact_id=parent_artifact_id,
                parent_artifact_version_id=parent_artifact_version_id,
            )
        except ArtifactBusy as exc:
            return ToolResult(status="failed", safe_summary=str(exc), error_type=exc.code)
        except ArtifactPayloadInvalid as exc:
            return ToolResult(status="failed", safe_summary=str(exc), error_type=exc.code)
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
    """

    name = "update_draft"
    input_model = UpdateDraftArgs
    points_cost = 0
    external_side_effect = True

    def __init__(self, db_session: AsyncSession | None = None) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = UpdateDraftArgs.model_validate(arguments)
        if self._db is None:
            return ToolResult(
                status="failed", safe_summary="update_draft requires a database session"
            )
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
            return ToolResult(status="failed", safe_summary=str(exc), error_type=exc.code)
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
]
