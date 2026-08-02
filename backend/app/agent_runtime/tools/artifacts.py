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


class CreateDraftTool:
    """创建（或复用稳定身份后继续）一个 Artifact Draft。

    ``business_fields`` 只含业务字段；稳定 ``artifact_key`` 由服务端生成，模型
    不能直接指定数据库 key（设计 §8.1）。子 Artifact（如 kol_analysis_v2）通过
    ``parent_artifact_version_id`` 固定到当时的父 Version。
    """

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
