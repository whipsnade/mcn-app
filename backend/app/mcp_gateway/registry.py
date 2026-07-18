from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.mcp_gateway.transport import DiscoveredTool, McpTransport
from app.mcp_gateway.validation import canonical_json_bytes, validate_schema_policy


class ToolNotEnabledError(LookupError):
    pass


@dataclass(frozen=True)
class ApprovedTool:
    catalog_id: str
    internal_name: str
    service: DataTapService
    remote_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


@dataclass(frozen=True)
class DiscoveryReport:
    service: DataTapService
    approved_remote_names: tuple[str, ...]
    quarantined_remote_names: tuple[str, ...]


@dataclass(frozen=True)
class _ManifestEntry:
    internal_name: str
    service: DataTapService
    remote_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    digest: str
    enabled: bool


# These names are reviewed product capabilities, not an open-ended remote
# discovery list. Their live input schemas are still checked and quarantined
# when the provider changes, while the stable descriptions keep untrusted
# provider text out of the model prompt.
DYNAMIC_TOOL_ALLOWLIST: dict[DataTapService, dict[str, tuple[str, str]]] = {
    DataTapService.INSIGHT_CUBE: {
        "match_best_tag": ("datatap.insight.match.best.tag.v1", "品牌与品类标准标签匹配"),
        "query_analysis_data": ("datatap.insight.query.analysis.v1", "品牌声量、互动、情感和平台维度统计"),
        "social_statistic_trend": ("datatap.insight.social.statistic.trend.v1", "品牌或关键词跨平台声量趋势"),
        "social_statistic_user_profile": ("datatap.insight.social.statistic.user.profile.v1", "品牌受众年龄、性别和地域画像"),
        "social_statistic_hot_user": ("datatap.insight.social.statistic.hot.user.v1", "品牌相关热门用户和传播达人"),
        "social_statistic_overview": ("datatap.insight.social.statistic.overview.v1", "品牌或关键词社交搜索整体概览"),
        "social_statistic_hot_topic": ("datatap.insight.social.statistic.hot.topic.v1", "品牌相关热门话题和声量聚类"),
    },
    DataTapService.SOCIAL_GROW: {
        "kol_match_mentions_tag": ("datatap.social.grow.kol.match.mentions.tag.v1", "品牌提及标签匹配"),
        "kol_detail": ("datatap.social.grow.kol.detail.v1", "指定平台达人详情与趋势画像"),
        # The legacy internal names remain stable for existing saved plans.
        "kol_xiaohongshu_search": ("datatap.xiaohongshu.kol.search.v1", "小红书 KOL 候选检索"),
        "kol_douyin_search": ("datatap.douyin.kol.search.v1", "抖音 KOL 候选检索"),
        "kol_bilibili_search": ("datatap.social.grow.kol.bilibili.search.v1", "B站 KOL 候选检索"),
        "kol_weibo_search": ("datatap.social.grow.kol.weibo.search.v1", "微博 KOL 候选检索"),
        "kol_wechat_search": ("datatap.social.grow.kol.wechat.search.v1", "微信 KOL 候选检索"),
    },
}

_DATATAP_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}


def close_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a safe copy of a provider schema with closed object nodes.

    DataTap's live discovery schemas are not guaranteed to include
    ``additionalProperties: false`` on every nested object.  The execution
    gateway deliberately rejects open object schemas so that model-generated
    arguments cannot contain undeclared fields.  Normalize the discovered
    schema at the registry boundary while keeping the original discovery
    payload/digest intact for change detection and quarantine decisions.
    """

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if result.get("type") == "object" or "properties" in result:
                result["additionalProperties"] = False
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    normalized = visit(deepcopy(dict(schema)))
    if not isinstance(normalized, dict):  # pragma: no cover - mapping input
        raise TypeError("schema must be an object")
    return normalized


def discovery_digest(tool: DiscoveredTool) -> str:
    payload = {
        "name": tool.name,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class ToolRegistryService:
    def __init__(
        self,
        db_session: AsyncSession,
        transport: McpTransport,
        *,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self._db = db_session
        self._transport = transport
        raw_manifest = dict(manifest) if manifest is not None else self._load_manifest()
        self._entries = self._parse_manifest(raw_manifest)
        self._by_internal = {entry.internal_name: entry for entry in self._entries}
        self._by_remote = {(entry.service, entry.remote_name): entry for entry in self._entries}
        self._dynamic_by_remote = {
            (service, remote): (internal_name, description)
            for service, tools in DYNAMIC_TOOL_ALLOWLIST.items()
            for remote, (internal_name, description) in tools.items()
        }
        self._dynamic_by_internal = {
            internal_name: (service, remote, description)
            for (service, remote), (internal_name, description) in self._dynamic_by_remote.items()
        }

    async def refresh_service(self, service: DataTapService) -> DiscoveryReport:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        discovered = await self._transport.list_tools(service)
        approved: list[str] = []
        quarantined: list[str] = []
        seen_remote_names: set[str] = set()
        now = datetime.now(UTC).replace(tzinfo=None)

        for tool in discovered:
            seen_remote_names.add(tool.name)
            entry = self._by_remote.get((service, tool.name))
            if entry is None and (service, tool.name) in self._dynamic_by_remote:
                await self._refresh_dynamic_tool(service, tool, now=now, approved=approved, quarantined=quarantined)
                continue
            if entry is None:
                await self._upsert_discovery(service, tool, review_status="quarantined", now=now)
                quarantined.append(tool.name)
                continue
            row = await self._row_by_internal(entry.internal_name)
            observed_digest = discovery_digest(tool)
            if observed_digest != entry.digest:
                await self._upsert_discovery(service, tool, review_status="quarantined", now=now)
                quarantined.append(tool.name)
                if row is not None:
                    row.review_status = "quarantined"
                    row.is_enabled = False
                    row.updated_at = now
                continue
            if row is None:
                row = McpToolCatalog(
                    id=str(uuid4()),
                    service_slug=entry.service.value,
                    internal_tool_name=entry.internal_name,
                    reviewed_description=entry.description,
                    input_schema_json=entry.input_schema,
                    output_validator_version="v1",
                    discovery_digest=entry.digest,
                    review_status="approved",
                    is_enabled=entry.enabled,
                    created_at=now,
                    updated_at=now,
                )
                self._db.add(row)
            elif (
                row.service_slug != entry.service.value
                or row.input_schema_json != entry.input_schema
                or row.discovery_digest != entry.digest
                or row.review_status != "approved"
            ):
                row.review_status = "quarantined"
                row.is_enabled = False
                row.updated_at = now
                quarantined.append(tool.name)
                await self._upsert_discovery(service, tool, review_status="quarantined", now=now)
                continue
            if not entry.enabled and row.is_enabled:
                row.is_enabled = False
                row.updated_at = now
            await self._upsert_discovery(service, tool, review_status="approved", now=now)
            approved.append(tool.name)

        for entry in self._entries:
            if entry.service != service or entry.remote_name in seen_remote_names:
                continue
            row = await self._row_by_internal(entry.internal_name)
            if row is not None:
                row.review_status = "quarantined"
                row.is_enabled = False
                row.updated_at = now
            quarantined.append(entry.remote_name)

        for dynamic_service, dynamic_tools in DYNAMIC_TOOL_ALLOWLIST.items():
            if dynamic_service != service:
                continue
            for remote_name, (internal_name, _description) in dynamic_tools.items():
                if remote_name in seen_remote_names:
                    continue
                row = await self._row_by_internal(internal_name)
                if row is not None:
                    row.review_status = "quarantined"
                    row.is_enabled = False
                    row.updated_at = now
                quarantined.append(remote_name)

        await self._db.flush()
        return DiscoveryReport(
            service=service,
            approved_remote_names=tuple(approved),
            quarantined_remote_names=tuple(dict.fromkeys(quarantined)),
        )

    async def require_enabled(self, internal_name: str) -> ApprovedTool:
        entry = self._by_internal.get(internal_name)
        if entry is None:
            dynamic = self._dynamic_by_internal.get(internal_name)
            if dynamic is None:
                raise ToolNotEnabledError("tool is not present in the approved manifest")
            service, remote_name, description = dynamic
            row = await self._row_by_internal(internal_name)
            if (
                row is None
                or not row.is_enabled
                or row.review_status != "approved"
                or row.service_slug != service.value
            ):
                raise ToolNotEnabledError("tool is not enabled")
            return ApprovedTool(
                catalog_id=row.id,
                internal_name=internal_name,
                service=service,
                remote_name=remote_name,
                input_schema=close_input_schema(row.input_schema_json),
                output_schema=_DATATAP_RESULT_SCHEMA,
            )
        row = await self._row_by_internal(internal_name)
        if (
            not entry.enabled
            or row is None
            or not row.is_enabled
            or row.review_status != "approved"
            or row.service_slug != entry.service.value
            or row.input_schema_json != entry.input_schema
            or row.discovery_digest != entry.digest
        ):
            raise ToolNotEnabledError("tool is not enabled")
        return self._approved_tool(row, entry)

    async def list_enabled(self) -> tuple[ApprovedTool, ...]:
        rows = (
            await self._db.scalars(
                select(McpToolCatalog)
                .where(
                    McpToolCatalog.is_enabled.is_(True),
                    McpToolCatalog.review_status == "approved",
                )
                .order_by(McpToolCatalog.internal_tool_name)
            )
        ).all()
        result: list[ApprovedTool] = []
        for row in rows:
            entry = self._by_internal.get(row.internal_tool_name)
            if entry is None:
                dynamic = self._dynamic_by_internal.get(row.internal_tool_name)
                if dynamic is not None and row.service_slug == dynamic[0].value:
                    result.append(
                        ApprovedTool(
                            catalog_id=row.id,
                            internal_name=row.internal_tool_name,
                            service=dynamic[0],
                            remote_name=dynamic[1],
                            input_schema=close_input_schema(row.input_schema_json),
                            output_schema=_DATATAP_RESULT_SCHEMA,
                        )
                    )
                continue
            if (
                entry is not None
                and entry.enabled
                and row.service_slug == entry.service.value
                and row.input_schema_json == entry.input_schema
                and row.discovery_digest == entry.digest
            ):
                result.append(self._approved_tool(row, entry))
        return tuple(result)

    async def _refresh_dynamic_tool(
        self,
        service: DataTapService,
        tool: DiscoveredTool,
        *,
        now: datetime,
        approved: list[str],
        quarantined: list[str],
    ) -> None:
        internal_name, description = self._dynamic_by_remote[(service, tool.name)]
        observed_digest = discovery_digest(tool)
        row = await self._row_by_internal(internal_name)
        if row is not None and row.discovery_digest != observed_digest:
            row.review_status = "quarantined"
            row.is_enabled = False
            row.updated_at = now
            await self._upsert_discovery(service, tool, review_status="quarantined", now=now)
            quarantined.append(tool.name)
            return
        if row is None:
            row = McpToolCatalog(
                id=str(uuid4()),
                service_slug=service.value,
                internal_tool_name=internal_name,
                reviewed_description=description,
                input_schema_json=tool.input_schema,
                output_validator_version="datatap_result_v1",
                discovery_digest=observed_digest,
                review_status="approved",
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
            self._db.add(row)
        await self._upsert_discovery(service, tool, review_status="approved", now=now)
        approved.append(tool.name)

    async def _row_by_internal(self, internal_name: str) -> McpToolCatalog | None:
        return await self._db.scalar(
            select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == internal_name)
        )

    async def _upsert_discovery(
        self,
        service: DataTapService,
        tool: DiscoveredTool,
        *,
        review_status: str,
        now: datetime,
    ) -> McpToolDiscovery:
        row = await self._db.scalar(
            select(McpToolDiscovery).where(
                McpToolDiscovery.service_slug == service.value,
                McpToolDiscovery.remote_name == tool.name,
            )
        )
        if row is None:
            row = McpToolDiscovery(
                id=str(uuid4()),
                service_slug=service.value,
                remote_name=tool.name,
                description=tool.description,
                input_schema_json=tool.input_schema,
                output_schema_json=tool.output_schema,
                discovery_digest=discovery_digest(tool),
                review_status=review_status,
                discovered_at=now,
                updated_at=now,
            )
            self._db.add(row)
            return row
        row.description = tool.description
        row.input_schema_json = tool.input_schema
        row.output_schema_json = tool.output_schema
        row.discovery_digest = discovery_digest(tool)
        row.review_status = review_status
        row.updated_at = now
        return row

    @staticmethod
    def _approved_tool(row: McpToolCatalog, entry: _ManifestEntry) -> ApprovedTool:
        return ApprovedTool(
            catalog_id=row.id,
            internal_name=row.internal_tool_name,
            service=entry.service,
            remote_name=entry.remote_name,
            input_schema=close_input_schema(entry.input_schema),
            output_schema=entry.output_schema,
        )

    @staticmethod
    def _load_manifest() -> dict[str, Any]:
        path = Path(__file__).with_name("approved_tools.json")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _parse_manifest(manifest: Mapping[str, Any]) -> tuple[_ManifestEntry, ...]:
        if manifest.get("manifest_version") != 1:
            raise ValueError("unsupported approved tool manifest version")
        tools = manifest.get("tools")
        if not isinstance(tools, list):
            raise ValueError("approved tool manifest tools must be a list")
        entries: list[_ManifestEntry] = []
        internal_names: set[str] = set()
        remote_keys: set[tuple[DataTapService, str]] = set()
        for raw in tools:
            if not isinstance(raw, dict):
                raise ValueError("approved tool manifest entry must be an object")
            try:
                service = DataTapService(raw["service"])
                internal_name = raw["internal_name"]
                remote_name = raw["remote_name"]
                description = raw["description"]
                input_schema = raw["input_schema"]
                output_schema = raw["output_schema"]
                digest = raw["discovery_digest"]
            except (KeyError, ValueError) as exc:
                raise ValueError("approved tool manifest entry is invalid") from exc
            if not all(
                isinstance(value, str) and value
                for value in (internal_name, remote_name, description, digest)
            ):
                raise ValueError("approved tool manifest names must be non-empty strings")
            if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
                raise ValueError("approved tool schemas must be objects")
            validate_schema_policy(input_schema, reject_routing_fields=True)
            validate_schema_policy(output_schema, reject_routing_fields=False)
            remote_key = (service, remote_name)
            if internal_name in internal_names or remote_key in remote_keys:
                raise ValueError("approved tool manifest contains duplicate entries")
            internal_names.add(internal_name)
            remote_keys.add(remote_key)
            entries.append(
                _ManifestEntry(
                    internal_name=internal_name,
                    service=service,
                    remote_name=remote_name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    digest=digest,
                    enabled=raw.get("enabled", True) is True,
                )
            )
        return tuple(entries)
