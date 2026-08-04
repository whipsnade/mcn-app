"""每轮模型决策上下文的组装器（设计文档 §九 / Task 14）。

``AgentEngine`` 每轮循环把"对话进行到此刻"的消息列表（原始用户输入 +
每轮 assistant 动作 + 工具结果）交给 ``decide()``。本构建器在对话消息前
拼一个由 Task 9 ``MemoryContextBuilder`` 组装的分层记忆头（当前用户消息、
最近消息、Session Summary、历史 Run 摘要、Artifact 紧凑目录、可用工具与
成本、钱包余额、去敏成功示例），构成模型可见的默认上下文。

不注入完整 Evidence / 完整历史报告 payload；模型按需通过历史读取工具钻取。
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.memory import (
    DEFAULT_RECENT_MESSAGE_WINDOW,
    DEFAULT_RUN_SUMMARY_LIMIT,
    MemoryContextBuilder,
)
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import AgentProfile
from app.agent_runtime.tools.registry import ToolRegistry
from app.model.contracts import ChatMessage


class SessionContextBuilder:
    """组装 decide() 每轮消息列表：分层记忆头 + 会话消息序列。"""

    def __init__(
        self,
        db: AsyncSession,
        registry: ToolRegistry,
        *,
        recent_message_window: int = DEFAULT_RECENT_MESSAGE_WINDOW,
        run_summary_limit: int = DEFAULT_RUN_SUMMARY_LIMIT,
    ) -> None:
        self._memory = MemoryContextBuilder(db, registry)
        self._recent_message_window = recent_message_window
        self._run_summary_limit = run_summary_limit

    async def build(
        self,
        *,
        run: AgentRun,
        profile: AgentProfile,
        conversation: list[ChatMessage],
        current_user_message: str = "",
        channel_permissions: tuple[str, ...] = (),
    ) -> list[ChatMessage]:
        """返回发送给模型的完整消息列表（不含 system prompt，网关会前插）。"""
        memory = await self._memory.build(
            user_id=run.user_id,
            session_id=run.session_id,
            profile=profile,
            current_user_message=current_user_message,
            channel_permissions=channel_permissions,
            recent_message_window=self._recent_message_window,
            run_summary_limit=self._run_summary_limit,
        )
        header = json.dumps(memory, ensure_ascii=False, default=str)
        return [ChatMessage(role="user", content=header), *conversation]


__all__ = ["SessionContextBuilder"]
