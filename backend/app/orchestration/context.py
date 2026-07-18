from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any, Protocol
import unicodedata

from app.orchestration.schemas import (
    PlannerContext,
    PlannerMessage,
    PlannerTool,
    SessionBrief,
)
from app.orchestration.analytics_contract import build_analytics_field_contract
from app.orchestration.export_contract import build_export_field_contract
from app.orchestration.routing import extract_requested_period


_OMIT = object()
_SENSITIVE_REPORT_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "endpoint",
    "host",
    "token",
    "url",
}
_SENSITIVE_REPORT_KEY_PARTS = {
    "api_key",
    "authorization",
    "credential",
    "endpoint",
    "host",
    "token",
    "url",
}
_DISABLED_SERVICE_NAMES = {
    "zhihu-mcp",
    "toutiao-mcp",
    "baidu-index-mcp",
    "google-trends-mcp",
}
_SUPPLIER_HOST_NAMES = {"datatap.deepminer.com.cn"}
_TEXT_SECRET_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:authorization|bearer|api[ _-]?key|token|credentials?)(?![a-z0-9_])",
    re.IGNORECASE,
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


def _normalized_key(key: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")


def _is_sensitive_report_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_REPORT_KEYS or any(
        part in normalized for part in _SENSITIVE_REPORT_KEY_PARTS
    )


def _contains_text_secret(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    return _TEXT_SECRET_PATTERN.search(normalized) is not None


def _project_reporting_value(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {
            key: child
            for key, item in value.items()
            if isinstance(key, str)
            and not _is_sensitive_report_key(key)
            and (child := _project_reporting_value(item)) is not _OMIT
        }
        return projected
    if isinstance(value, list):
        return [
            child
            for item in value
            if (child := _project_reporting_value(item)) is not _OMIT
        ]
    if isinstance(value, str):
        value_lower = value.casefold()
        if (
            any(service_name in value_lower for service_name in _DISABLED_SERVICE_NAMES)
            or any(host_name in value_lower for host_name in _SUPPLIER_HOST_NAMES)
            or "http://" in value_lower
            or "https://" in value_lower
            or _contains_text_secret(value)
        ):
            return _OMIT
    return value


def project_reporting_summary(summary: dict[str, Any]) -> dict[str, Any]:
    projected = _project_reporting_value(summary)
    if not isinstance(projected, dict):
        raise TypeError("reporting context summary must be an object")
    return projected


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
        selected_channels = tuple(
            platform for platform in workspace.platforms if platform in approved_channels
        )
        effective_channels = selected_channels or tuple(sorted(approved_channels))
        brief = SessionBrief.from_workspace(workspace).model_copy(
            update={"platforms": effective_channels}
        )
        recent_messages = compress_messages(messages, max_chars=24_000)
        return PlannerContext(
            brief=brief,
            recent_messages=recent_messages,
            existing_results=project_reporting_summary(
                await self.reporting.context_summary(session_id)
            ),
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
            # 渠道在新建会话中是可选条件。未显式选择时应在用户已授权的
            # 全部渠道内规划；只有显式选择时才收窄到用户的选择。
            allowed_channels=effective_channels,
            export_contract=build_export_field_contract(brief),
            analytics_contract=build_analytics_field_contract(),
            # Scope and objectives are model decisions.  ``None`` makes sure
            # the provider cannot mistake a precomputed scope for a mandate.
            analysis_scope=None,
            analysis_objectives=(),
            requested_period=extract_requested_period(
                "\n".join(message.content for message in recent_messages)
            ),
        )
