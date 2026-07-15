from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from app.orchestration.schemas import (
    PlannerContext,
    PlannerMessage,
    PlannerTool,
    SessionBrief,
)


class WorkspaceReader(Protocol):
    async def get_owned_session(self, user_id: str, session_id: str) -> Any: ...

    async def list_messages(self, user_id: str, session_id: str) -> Sequence[Any]: ...


class ToolDirectory(Protocol):
    async def list_enabled(self) -> Sequence[Any]: ...


class ChannelPermissionReader(Protocol):
    async def list_enabled_channels(self, user_id: str) -> Sequence[str]: ...


class ReportingContextReader(Protocol):
    async def context_summary(self, session_id: str) -> dict[str, Any]: ...


def compress_messages(messages: Sequence[Any], *, max_chars: int) -> tuple[PlannerMessage, ...]:
    """保留最新消息，且不让任何消息绕过规划 Prompt 的长度边界。"""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    remaining = max_chars
    selected: list[PlannerMessage] = []
    for message in reversed(messages):
        if remaining <= 0:
            break
        content = str(message.content)
        if len(content) > remaining:
            content = content[-remaining:]
        if content:
            selected.append(
                PlannerMessage(
                    role=message.role,
                    content=content,
                    sequence=message.sequence,
                )
            )
            remaining -= len(content)
    return tuple(reversed(selected))


class ContextBuilder:
    def __init__(
        self,
        *,
        workspace: WorkspaceReader,
        registry: ToolDirectory,
        permissions: ChannelPermissionReader,
        reporting: ReportingContextReader,
    ) -> None:
        self.workspace = workspace
        self.registry = registry
        self.permissions = permissions
        self.reporting = reporting

    async def build(self, user_id: str, session_id: str) -> PlannerContext:
        workspace = await self.workspace.get_owned_session(user_id, session_id)
        messages = await self.workspace.list_messages(user_id, session_id)
        approved_channels = set(await self.permissions.list_enabled_channels(user_id))
        tools = await self.registry.list_enabled()
        return PlannerContext(
            brief=SessionBrief.from_workspace(workspace),
            recent_messages=compress_messages(messages, max_chars=24_000),
            existing_results=await self.reporting.context_summary(session_id),
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
            allowed_channels=tuple(
                platform for platform in workspace.platforms if platform in approved_channels
            ),
        )
