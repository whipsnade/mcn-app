"""分层记忆上下文构建器（设计文档 §九）。

Session Agent 每轮默认获得：当前用户消息、最近有限条消息、Session Summary、
历史 Run 摘要、紧凑 Artifact 目录（类型/版本/范围/父子关系/数据状态）、
可用工具与成本、钱包余额。

**默认上下文绝不注入完整 Evidence 或完整历史报告 payload**；模型按需调用
历史读取工具（read_artifact / search_evidence / read_tool_result）钻取。
跨用户读取他人 Session 的上下文一律拒绝（:class:`MemorySessionForbidden`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.models import AgentMessage, AgentSession, MemoryEntry
from app.agent_runtime.profiles import AgentProfile
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.service import WalletService

# 默认最近消息窗口大小（§九「最近有限条消息」）。
DEFAULT_RECENT_MESSAGE_WINDOW = 8
# 默认历史 Run 摘要上限：与最近消息窗口一致，防止长 Session 摘要无限增长。
DEFAULT_RUN_SUMMARY_LIMIT = 20


class MemorySessionNotFound(LookupError):
    """Session 不存在。"""


class MemorySessionForbidden(PermissionError):
    """Session 属于其他用户，拒绝读取上下文。"""


class MemoryContextBuilder:
    """组装 Session Agent 每轮默认上下文的构建器。

    只读查询 agent_sessions / agent_messages / memory_entries / agent_artifacts /
    agent_artifact_versions，并通过 ToolRegistry 获取 Profile 可见工具目录与
    WalletService 获取钱包余额。不写任何数据。
    """

    def __init__(self, db: AsyncSession, registry: ToolRegistry) -> None:
        self._db = db
        self._registry = registry

    async def build(
        self,
        *,
        user_id: str,
        session_id: str,
        profile: AgentProfile,
        current_user_message: str,
        channel_permissions: Iterable[str] = (),
        recent_message_window: int = DEFAULT_RECENT_MESSAGE_WINDOW,
        run_summary_limit: int = DEFAULT_RUN_SUMMARY_LIMIT,
    ) -> dict[str, Any]:
        """组装默认上下文；Session 缺失或属他人时抛 :class:`LookupError`/异常。"""
        session = await self._db.get(AgentSession, session_id)
        if session is None:
            raise MemorySessionNotFound("agent_session_not_found")
        if session.user_id != user_id:
            raise MemorySessionForbidden("agent_session_forbidden")

        recent = await self._recent_messages(session_id, recent_message_window)
        run_summaries = await self._run_summaries(session_id, run_summary_limit)
        artifact_directory = await self._artifact_directory(session_id)
        tools = await self._registry.visible_tools(profile, channel_permissions=channel_permissions)

        return {
            "current_user_message": current_user_message,
            "recent_messages": recent,
            "session_summary": session.session_summary,
            "run_summaries": run_summaries,
            "artifact_directory": artifact_directory,
            "available_tools": [
                {
                    "internal_name": entry.internal_name,
                    "category": entry.category,
                    "points_cost": entry.points_cost,
                    "description": entry.description,
                    # 模型必须看到工具输入 Schema 才能构造合法参数（设计 §九/§10）；
                    # 见 registry.RegisteredTool.input_schema。
                    "input_schema": entry.input_schema,
                }
                for entry in tools
            ],
            "wallet": await self._wallet_balance(user_id),
        }

    async def _recent_messages(self, session_id: str, window: int) -> list[dict[str, Any]]:
        rows = (
            await self._db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.sequence.desc())
                .limit(max(window, 1))
            )
        ).all()
        rows = list(reversed(rows))
        return [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "sequence": message.sequence,
            }
            for message in rows
        ]

    async def _run_summaries(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        # 只取最近 limit 条（created_at 降序 + limit，再反转为时间正序），
        # 防止长 Session 的摘要无限挤占每轮上下文。
        entries = list(
            reversed(
                (
                    await self._db.scalars(
                        select(MemoryEntry)
                        .where(
                            MemoryEntry.session_id == session_id,
                            MemoryEntry.memory_type == "run_summary",
                            MemoryEntry.superseded_at.is_(None),
                        )
                        .order_by(MemoryEntry.created_at.desc())
                        .limit(max(limit, 1))
                    )
                ).all()
            )
        )
        summaries: list[dict[str, Any]] = []
        for entry in entries:
            content = dict(entry.content_json or {})
            content["memory_id"] = entry.id
            content["source_run_id"] = entry.source_run_id
            summaries.append(content)
        return summaries

    async def _artifact_directory(self, session_id: str) -> list[dict[str, Any]]:
        artifacts = (
            await self._db.scalars(
                select(AgentArtifact)
                .where(AgentArtifact.session_id == session_id)
                .order_by(AgentArtifact.activity_sequence.asc(), AgentArtifact.created_at.asc())
            )
        ).all()
        if not artifacts:
            return []
        # 只投影目录所需列（artifact_id/version/data_status），绝不加载大字段
        # payload_json / evidence_refs_json（§九「紧凑目录」）。
        version_result = await self._db.execute(
            select(
                AgentArtifactVersion.artifact_id,
                AgentArtifactVersion.version,
                AgentArtifactVersion.data_status,
            ).where(
                AgentArtifactVersion.artifact_id.in_([artifact.id for artifact in artifacts])
            )
        )
        version_rows = version_result.all()
        latest: dict[str, Any] = {}
        for version in version_rows:
            current = latest.get(version.artifact_id)
            if current is None or version.version > current.version:
                latest[version.artifact_id] = version

        directory: list[dict[str, Any]] = []
        for artifact in artifacts:
            latest_version = latest.get(artifact.id)
            directory.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_key": artifact.artifact_key,
                    "module": artifact.module,
                    "artifact_type": artifact.artifact_type,
                    "version": artifact.latest_version,
                    "parent_artifact_id": artifact.parent_artifact_id,
                    "status": artifact.status,
                    "data_status": latest_version.data_status if latest_version else None,
                    "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
                }
            )
        return directory

    async def _wallet_balance(self, user_id: str) -> dict[str, int]:
        try:
            wallet = await WalletService(self._db).get_wallet(user_id)
        except LookupError:
            return {"balance": 0, "reserved": 0}
        return {"balance": wallet.balance, "reserved": wallet.reserved}


__all__ = [
    "DEFAULT_RECENT_MESSAGE_WINDOW",
    "DEFAULT_RUN_SUMMARY_LIMIT",
    "MemoryContextBuilder",
    "MemorySessionForbidden",
    "MemorySessionNotFound",
]
