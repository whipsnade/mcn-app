"""统一 Tool Registry（设计文档 §十「工具运行时」/ §16「安全与审计」）。

注册所有可被 Agent 调用的可信工具，并在模型可见性上做三重求交（§16）：

1. **Profile 分类**：工具按分类（MCP / history / calculation / artifact /
   kol_detail，见 profiles.TOOL_CATEGORIES）注册，只有 Profile
   ``allowed_tool_categories`` 允许的分类可见；Profile 还可声明
   ``mcp_tool_allowlist``（如 kol_detail_v1 的达人详情/热帖名单，设计
   §5.1），此时 MCP 工具进一步按内部名名单过滤；
2. **实时审核状态**：MCP 工具来自 ``mcp_tool_catalog``（通过注入的目录源），
   只有 ``review_status == "approved"`` 且 ``is_enabled`` 可见；执行路径
   另经注入的 ``catalog_lookup`` 在 dispatch 前按行实时复核（存在 /
   approved / enabled / 签名 digest 与装配时一致），装配后管理员撤销、
   隔离、禁用或签名漂移都会被拦截（修复设计 §5.1）；
3. **用户渠道权限**：MCP 工具按其服务对应渠道，与用户的渠道权限求交。

执行时服务端上下文（``user_id/session_id/run_id/profile_name``）通过
:class:`ToolContext` 注入；模型参数中的服务端保留键在进入工具前被剥离（§16）。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.agent_runtime.profiles import AgentProfile, MCP_TOOLS, TOOL_CATEGORIES
from app.agent_runtime.tools.contracts import (
    SERVER_RESERVED_KEYS,
    TOOL_ARGUMENTS_INVALID,
    ToolContext,
    ToolResult,
    TrustedTool,
    format_validation_error,
)
from app.mcp_gateway.registry import close_input_schema


class ToolContractError(ValueError):
    """工具不满足 TrustedTool 契约，或注册/目录冲突。"""


class UnknownToolError(LookupError):
    """internal_name 未注册或当前不可执行。"""


class CatalogRow(Protocol):
    """``mcp_tool_catalog`` 行的最小形状（McpToolCatalog ORM 行或轻量投影）。

    两种来源（ORM 行与 :class:`McpCatalogEntry` 快照）字段一致，可互换注入。
    """

    internal_tool_name: str
    service_slug: str
    reviewed_description: str
    input_schema_json: dict[str, Any]
    review_status: str
    is_enabled: bool
    discovery_digest: str


@dataclass(frozen=True)
class McpCatalogEntry:
    """MCP 目录行的内存快照，用于纯单元测试注入。"""

    internal_tool_name: str
    service_slug: str
    reviewed_description: str
    input_schema_json: dict[str, Any]
    review_status: str
    is_enabled: bool
    # 实时发现签名；缺省空串仅为兼容旧测试构造，生产行（NOT NULL 列）必有值。
    discovery_digest: str = ""


@dataclass(frozen=True)
class RegisteredTool:
    """注册表内一条工具描述。

    - ``category``：TOOL_CATEGORIES 词汇表分类（§五）；
    - ``channel``：MCP 工具所需渠道权限，``None`` 表示跨平台/无渠道门槛；
    - ``tool``：可执行对象；目录来源的 MCP 工具在 Task 8 接入执行器前为 ``None``；
    - ``review_status`` / ``is_enabled`` / ``discovery_digest``：仅目录来源的
      MCP 工具携带；``discovery_digest`` 是装配时的实时发现签名，执行前复核
      用于检测签名漂移。
    """

    internal_name: str
    category: str
    points_cost: int
    external_side_effect: bool
    description: str = ""
    channel: str | None = None
    input_model: type[BaseModel] | None = None
    tool: TrustedTool | None = None
    review_status: str | None = None
    is_enabled: bool = True
    discovery_digest: str | None = None
    # 模型可见的输入 JSON Schema（§九/§10：模型需要看到 Schema 才能构造合法参数）。
    # 静态工具取 ``input_model.model_json_schema()``；目录 MCP 工具取实时发现并
    # 封闭后的 ``input_schema_json``。
    input_schema: dict[str, Any] | None = None


# service_slug -> 该服务工具所需的渠道权限；None 表示跨平台、无渠道门槛。
_MCP_SERVICE_CHANNEL: dict[str, str | None] = {
    "insight-cube-mcp": None,
    "social-grow-mcp": None,  # 多平台服务，按内部工具名细分（见下）
    "social-grow-content-mcp": "xiaohongshu",
    "aktools-mcp": None,
    "bilibili-mcp": "bilibili",
}

# social-grow 多平台服务按内部工具名细分渠道。
_MCP_INTERNAL_CHANNEL: dict[str, str] = {
    "kol_xiaohongshu_search": "xiaohongshu",
    "kol_douyin_search": "douyin",
    "kol_bilibili_search": "bilibili",
    "kol_weibo_search": "weibo",
    "kol_wechat_search": "wechat",
}

# §11.3：每次 DataTap MCP 调用固定 10 积分。
_MCP_POINTS_COST = 10


def _validate_tool(tool: TrustedTool) -> None:
    """结构校验 TrustedTool 契约；失败抛 :class:`ToolContractError`。"""
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        raise ToolContractError("tool.name must be a non-empty string")
    input_model = getattr(tool, "input_model", None)
    if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
        raise ToolContractError("tool.input_model must be a Pydantic BaseModel subclass")
    points_cost = getattr(tool, "points_cost", None)
    if not isinstance(points_cost, int) or points_cost < 0:
        raise ToolContractError("tool.points_cost must be a non-negative int")
    if not isinstance(getattr(tool, "external_side_effect", None), bool):
        raise ToolContractError("tool.external_side_effect must be a bool")
    execute = getattr(tool, "execute", None)
    if not callable(execute) or not inspect.iscoroutinefunction(execute):
        raise ToolContractError("tool.execute must be an async callable")
    reserved = SERVER_RESERVED_KEYS & input_model.model_fields.keys()
    if reserved:
        raise ToolContractError(
            f"tool.input_model must not declare server-reserved keys: {sorted(reserved)}"
        )


def _channel_for_catalog_row(row: CatalogRow) -> str | None:
    """解析目录行所需渠道：先按内部工具名细分，再按服务回退。

    Task 7 硬化：未知 service_slug 一律抛错（fail-closed），而不是默认放行无渠道
    门槛。已知 slug 映射为 None 才表示跨平台、无渠道门槛。
    """
    channel = _MCP_INTERNAL_CHANNEL.get(row.internal_tool_name)
    if channel is None:
        if row.service_slug not in _MCP_SERVICE_CHANNEL:
            raise ToolContractError(f"unknown service_slug: {row.service_slug!r}")
        channel = _MCP_SERVICE_CHANNEL.get(row.service_slug)
    return channel


class ToolRegistry:
    """统一工具注册表：注册 + Profile/渠道/审核状态过滤 + 服务端上下文注入执行。"""

    def __init__(
        self,
        *,
        catalog_source: (
            Iterable[CatalogRow]
            | Callable[[], Iterable[CatalogRow] | Awaitable[Iterable[CatalogRow]]]
            | None
        ) = None,
        mcp_executor_factory: Callable[[CatalogRow], TrustedTool | None] | None = None,
        catalog_lookup: (
            Callable[[str], CatalogRow | None | Awaitable[CatalogRow | None]] | None
        ) = None,
    ) -> None:
        self._entries: dict[str, RegisteredTool] = {}
        self._catalog_source = catalog_source
        # Task 8：目录来源的 MCP 工具在注册时经该工厂挂上 AgentMcpTool 执行器；
        # 引擎在接线时注入（内部名 → service/remote/schema 由 mcp_gateway 解析）。
        self._mcp_executor_factory = mcp_executor_factory
        # G2：执行前按 internal_tool_name 实时查询目录行（轻量单行查询），
        # 复核装配后管理员是否撤销/隔离/禁用或签名漂移；为 None 时跳过复核，
        # 保持纯内存装配（单测）既有语义。
        self._catalog_lookup = catalog_lookup
        self._catalog_loaded = False

    def register(self, tool: TrustedTool, *, category: str) -> RegisteredTool:
        """注册一个可信工具。

        MCP 工具不通过 register 注册（一律来自审核目录），分类必须是
        TOOL_CATEGORIES 词汇表成员，internal_name 不得重复。
        """
        if category not in TOOL_CATEGORIES:
            raise ToolContractError(f"unknown tool category: {category!r}")
        if category == MCP_TOOLS:
            raise ToolContractError(
                "MCP tools are sourced from the approved catalog, not register()"
            )
        _validate_tool(tool)
        if tool.name in self._entries:
            raise ToolContractError(f"duplicate tool internal_name: {tool.name!r}")
        # 静态工具的描述优先取工具显式声明的 ``description``，否则回退到 docstring
        # 首行；模型上下文里的 available_tools 依赖该描述理解工具用途（UAT 发现：
        # 空描述导致模型猜错 create_draft 的 module 取值）。
        tool_description = (
            getattr(tool, "description", None)
            or (((tool.__doc__ or "").strip().splitlines() or [""])[0])
        )
        entry = RegisteredTool(
            internal_name=tool.name,
            category=category,
            points_cost=tool.points_cost,
            external_side_effect=tool.external_side_effect,
            description=tool_description,
            input_model=tool.input_model,
            tool=tool,
            input_schema=tool.input_model.model_json_schema(),
        )
        self._entries[tool.name] = entry
        return entry

    @property
    def registered_tools(self) -> tuple[RegisteredTool, ...]:
        """当前已注册的工具集合（含已加载的 MCP 目录工具）。"""
        return tuple(sorted(self._entries.values(), key=lambda entry: entry.internal_name))

    async def visible_tools(
        self,
        profile: AgentProfile,
        *,
        channel_permissions: Iterable[str] = (),
    ) -> tuple[RegisteredTool, ...]:
        """返回 Profile + 用户渠道权限 + 实时审核状态共同允许的工具。"""
        await self._ensure_catalog()
        granted = frozenset(channel_permissions)
        result: list[RegisteredTool] = []
        for entry in self._entries.values():
            if entry.category not in profile.allowed_tool_categories:
                continue
            if entry.category == MCP_TOOLS:
                if profile.mcp_tool_allowlist is not None and (
                    entry.internal_name not in profile.mcp_tool_allowlist
                ):
                    continue
                if entry.review_status != "approved" or not entry.is_enabled:
                    continue
                if entry.channel is not None and entry.channel not in granted:
                    continue
            result.append(entry)
        return tuple(sorted(result, key=lambda entry: entry.internal_name))

    async def execute(
        self,
        *,
        internal_name: str,
        arguments: Mapping[str, Any],
        user_id: str,
        session_id: str,
        run_id: str,
        profile: AgentProfile,
        channel_permissions: Iterable[str] = (),
        step_id: str | None = None,
    ) -> ToolResult:
        """构建服务端 ToolContext、剥离保留键后调用工具。

        模型提供的参数在进入工具前剥离 ``user_id/session_id/run_id/step_id``
        （§16），实际身份始终来自服务端注入的 ``ToolContext``。
        Task 7 硬化 (b)：执行前重新校验工具对当前 Profile 可见（Profile 分类 +
        实时审核状态 + 用户渠道权限求交），防止审核撤销后仍被执行。
        G2：MCP 工具在 dispatch 前再经 ``catalog_lookup`` 实时复核目录行
        （存在 / approved / enabled / 签名与装配时一致）——装配缓存可能被
        管理员事后变更绕过，实时复核在 ``entry.tool.execute``（AgentMcpTool
        的 prepare/积分预留）之前执行，复核失败抛 :class:`UnknownToolError`
        （与静态检查同一出口），不产生任何预留，无需释放。
        H3：``input_model`` 参数校验失败（模型编造字段/缺必填项）不冒泡，
        统一转为 ``tool_arguments_invalid`` 结构化回喂（字段级明细、截断到
        上限），校验在 dispatch 之前，工具零副作用。
        """
        await self._ensure_catalog()
        entry = self._entries.get(internal_name)
        if entry is None or entry.tool is None:
            raise UnknownToolError(f"tool is not registered or not executable: {internal_name!r}")
        if entry.category not in profile.allowed_tool_categories:
            raise UnknownToolError(f"tool is not allowed by profile: {internal_name!r}")
        if entry.category == MCP_TOOLS:
            if profile.mcp_tool_allowlist is not None and (
                entry.internal_name not in profile.mcp_tool_allowlist
            ):
                raise UnknownToolError(
                    f"tool is not in profile mcp allowlist: {internal_name!r}"
                )
            if entry.review_status != "approved" or not entry.is_enabled:
                raise UnknownToolError(f"tool is not approved or enabled: {internal_name!r}")
            if entry.channel is not None and entry.channel not in frozenset(channel_permissions):
                raise UnknownToolError(f"tool requires channel: {entry.channel!r}")
            await self._recheck_mcp_catalog_row(entry)
        scrubbed = {
            key: value for key, value in arguments.items() if key not in SERVER_RESERVED_KEYS
        }
        parsed: BaseModel | Mapping[str, Any]
        if entry.input_model is not None:
            try:
                parsed = entry.input_model.model_validate(scrubbed)
            except ValidationError as exc:
                # H3：参数校验失败必须结构化回喂模型（字段级明细），而不是冒泡为
                # engine 级「failed unexpectedly」——语义同 MCP 侧
                # definitely_not_sent：校验在 dispatch 之前，工具零副作用零计费，
                # 模型按明细修正参数后重试即可自愈。
                return ToolResult(
                    status="failed",
                    safe_summary=format_validation_error(
                        exc, prefix=f"invalid arguments for tool {internal_name!r}: "
                    ),
                    error_type=TOOL_ARGUMENTS_INVALID,
                )
        else:
            parsed = scrubbed
        context = ToolContext(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            profile_name=profile.full_name,
            step_id=step_id,
        )
        return await entry.tool.execute(context, parsed)

    async def _recheck_mcp_catalog_row(self, entry: RegisteredTool) -> None:
        """执行前实时复核 MCP 目录行（G2，修复设计 §5.1）。

        注入 ``catalog_lookup`` 时按 internal_tool_name 做一次轻量单行查询，
        复核：行仍存在、``review_status == "approved"``、``is_enabled``、实时
        签名 ``discovery_digest`` 与装配时一致（防止签名漂移后继续调用）。
        未注入（纯内存装配）时跳过，保持既有语义。
        """
        if self._catalog_lookup is None:
            return
        live = self._catalog_lookup(entry.internal_name)
        if inspect.isawaitable(live):
            live = await live
        if live is None:
            raise UnknownToolError(
                f"tool catalog row missing at execution time: {entry.internal_name!r}"
            )
        if live.review_status != "approved" or not live.is_enabled:
            raise UnknownToolError(
                f"tool not approved/enabled at execution time: {entry.internal_name!r}"
            )
        if live.discovery_digest != entry.discovery_digest:
            raise UnknownToolError(
                f"tool signature drifted since assembly: {entry.internal_name!r}"
            )

    async def reload_catalog(self) -> None:
        """重读目录源并替换 MCP 条目，审核状态不被无限期缓存（Task 7 硬化 a）。

        引擎可周期性调用，或在关键操作前强制刷新。
        """
        for name in [
            name
            for name, entry in self._entries.items()
            if entry.category == MCP_TOOLS
        ]:
            del self._entries[name]
        self._catalog_loaded = False
        await self._ensure_catalog()

    async def _ensure_catalog(self) -> None:
        """加载一次注入的 MCP 目录源（快照或 DB 查询），并把批准启用工具纳入注册表。"""
        if self._catalog_loaded:
            return
        if self._catalog_source is not None:
            raw = self._catalog_source() if callable(self._catalog_source) else self._catalog_source
            if inspect.isawaitable(raw):
                raw = await raw
            for row in raw:
                self._register_catalog_row(row)
        self._catalog_loaded = True

    def _register_catalog_row(self, row: CatalogRow) -> None:
        internal_name = row.internal_tool_name
        if internal_name in self._entries:
            raise ToolContractError(f"duplicate tool internal_name: {internal_name!r}")
        executor = (
            self._mcp_executor_factory(row)
            if self._mcp_executor_factory is not None
            else None
        )
        self._entries[internal_name] = RegisteredTool(
            internal_name=internal_name,
            category=MCP_TOOLS,
            points_cost=_MCP_POINTS_COST,
            external_side_effect=True,
            description=row.reviewed_description,
            channel=_channel_for_catalog_row(row),
            input_model=None,
            tool=executor,
            review_status=row.review_status,
            is_enabled=row.is_enabled,
            discovery_digest=row.discovery_digest,
            input_schema=close_input_schema(row.input_schema_json),
        )


__all__ = [
    "CatalogRow",
    "McpCatalogEntry",
    "RegisteredTool",
    "ToolContractError",
    "ToolRegistry",
    "UnknownToolError",
]
