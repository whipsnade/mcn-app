from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpToolCatalog
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
            if entry is None:
                quarantined.append(tool.name)
                continue
            row = await self._row_by_internal(entry.internal_name)
            observed_digest = discovery_digest(tool)
            if observed_digest != entry.digest:
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
                continue
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

        await self._db.flush()
        return DiscoveryReport(
            service=service,
            approved_remote_names=tuple(approved),
            quarantined_remote_names=tuple(dict.fromkeys(quarantined)),
        )

    async def require_enabled(self, internal_name: str) -> ApprovedTool:
        entry = self._by_internal.get(internal_name)
        if entry is None:
            raise ToolNotEnabledError("tool is not present in the approved manifest")
        row = await self._row_by_internal(internal_name)
        if (
            row is None
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
            if (
                entry is not None
                and row.service_slug == entry.service.value
                and row.input_schema_json == entry.input_schema
                and row.discovery_digest == entry.digest
            ):
                result.append(self._approved_tool(row, entry))
        return tuple(result)

    async def _row_by_internal(self, internal_name: str) -> McpToolCatalog | None:
        return await self._db.scalar(
            select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == internal_name)
        )

    @staticmethod
    def _approved_tool(row: McpToolCatalog, entry: _ManifestEntry) -> ApprovedTool:
        return ApprovedTool(
            catalog_id=row.id,
            internal_name=row.internal_tool_name,
            service=entry.service,
            remote_name=entry.remote_name,
            input_schema=entry.input_schema,
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
