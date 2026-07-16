from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.mcp_gateway.registry import (
    ToolNotEnabledError,
    ToolRegistryService,
    discovery_digest,
)
from app.mcp_gateway.transport import DiscoveredTool

from tests.mcp_gateway.fakes import strict_object_schema


async def _registry_with_manifest_disabled_tool(db_session):
    input_schema = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
    output_schema = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")
    discovered = DiscoveredTool(
        name="search-creators",
        description="approved but disabled",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    row = McpToolCatalog(
        id=str(uuid4()),
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        reviewed_description="approved but disabled",
        input_schema_json=input_schema,
        output_validator_version="v1",
        discovery_digest=discovery_digest(discovered),
        review_status="approved",
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()
    manifest = {
        "manifest_version": 1,
        "tools": [
            {
                "internal_name": row.internal_tool_name,
                "service": row.service_slug,
                "remote_name": discovered.name,
                "description": row.reviewed_description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "discovery_digest": row.discovery_digest,
                "enabled": False,
            }
        ],
    }
    transport = FakeMcpTransport.with_discovered_tool(
        service=DataTapService.SOCIAL_GROW,
        remote_name=discovered.name,
        input_schema=input_schema,
        output_schema=output_schema,
        description=discovered.description,
    )
    return row, ToolRegistryService(db_session, transport, manifest=manifest)


async def test_schema_drift_quarantines_without_overwriting_approved_schema(
    db_session,
) -> None:
    approved_schema = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
    output_schema = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")
    approved_discovery = DiscoveredTool(
        name="search-creators",
        description="approved",
        input_schema=approved_schema,
        output_schema=output_schema,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    row = McpToolCatalog(
        id=str(uuid4()),
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        reviewed_description="approved",
        input_schema_json=approved_schema,
        output_validator_version="v1",
        discovery_digest=discovery_digest(approved_discovery),
        review_status="approved",
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()
    manifest = {
        "manifest_version": 1,
        "tools": [
            {
                "internal_name": row.internal_tool_name,
                "service": row.service_slug,
                "remote_name": approved_discovery.name,
                "description": row.reviewed_description,
                "input_schema": approved_schema,
                "output_schema": output_schema,
                "discovery_digest": row.discovery_digest,
            }
        ],
    }
    transport = FakeMcpTransport.with_discovered_tool(
        service=DataTapService.SOCIAL_GROW,
        remote_name=approved_discovery.name,
        input_schema=strict_object_schema({"url": {"type": "string"}}, "url"),
        output_schema=output_schema,
    )

    report = await ToolRegistryService(db_session, transport, manifest=manifest).refresh_service(
        DataTapService.SOCIAL_GROW
    )
    await db_session.refresh(row)

    assert report.quarantined_remote_names == (approved_discovery.name,)
    assert row.review_status == "quarantined"
    assert row.is_enabled is False
    assert row.input_schema_json == approved_schema
    assert row.discovery_digest == manifest["tools"][0]["discovery_digest"]
    audit = await db_session.scalar(
        select(McpToolDiscovery).where(
            McpToolDiscovery.service_slug == DataTapService.SOCIAL_GROW.value,
            McpToolDiscovery.remote_name == approved_discovery.name,
        )
    )
    assert audit is not None
    assert audit.review_status == "quarantined"
    assert audit.input_schema_json == strict_object_schema({"url": {"type": "string"}}, "url")


async def test_unmanifested_tool_is_quarantined_and_not_added_to_catalog(db_session) -> None:
    transport = FakeMcpTransport.with_discovered_tool(
        service=DataTapService.BILIBILI,
        remote_name="surprise-tool",
        input_schema=strict_object_schema({}),
        output_schema=strict_object_schema({}),
    )

    report = await ToolRegistryService(db_session, transport).refresh_service(
        DataTapService.BILIBILI
    )

    assert report.quarantined_remote_names == ("surprise-tool",)
    assert (await db_session.scalars(select(McpToolCatalog))).all() == []
    audit = await db_session.scalar(
        select(McpToolDiscovery).where(
            McpToolDiscovery.service_slug == DataTapService.BILIBILI.value,
            McpToolDiscovery.remote_name == "surprise-tool",
        )
    )
    assert audit is not None
    assert audit.review_status == "quarantined"
    assert audit.description is None


async def test_unmanifested_tool_discovery_is_upserted_without_internal_name(
    db_session,
) -> None:
    first_schema = strict_object_schema({"keyword": {"type": "string"}})
    transport = FakeMcpTransport.with_discovered_tool(
        service=DataTapService.BILIBILI,
        remote_name="surprise-tool",
        input_schema=first_schema,
        output_schema=None,
        description="first",
    )
    registry = ToolRegistryService(db_session, transport)
    await registry.refresh_service(DataTapService.BILIBILI)
    first = await db_session.scalar(select(McpToolDiscovery))
    assert first is not None
    first_id = first.id
    discovered_at = first.discovered_at

    changed_schema = strict_object_schema({"keyword": {"type": "integer"}})
    transport.discovered_tools[DataTapService.BILIBILI] = (
        DiscoveredTool(
            name="surprise-tool",
            description="changed",
            input_schema=changed_schema,
            output_schema=None,
        ),
    )
    await registry.refresh_service(DataTapService.BILIBILI)
    db_session.expire_all()
    rows = (await db_session.scalars(select(McpToolDiscovery))).all()

    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].discovered_at == discovered_at
    assert rows[0].description == "changed"
    assert rows[0].input_schema_json == changed_schema
    assert rows[0].review_status == "quarantined"


async def test_remote_rename_quarantines_and_disables_stale_approved_row(
    db_session,
) -> None:
    input_schema = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
    output_schema = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")
    approved = DiscoveredTool(
        name="search-creators",
        description="approved",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    row = McpToolCatalog(
        id=str(uuid4()),
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        reviewed_description="approved",
        input_schema_json=input_schema,
        output_validator_version="v1",
        discovery_digest=discovery_digest(approved),
        review_status="approved",
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()
    manifest = {
        "manifest_version": 1,
        "tools": [
            {
                "internal_name": row.internal_tool_name,
                "service": row.service_slug,
                "remote_name": approved.name,
                "description": row.reviewed_description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "discovery_digest": row.discovery_digest,
            }
        ],
    }
    transport = FakeMcpTransport.with_discovered_tool(
        service=DataTapService.SOCIAL_GROW,
        remote_name="renamed-search-creators",
        input_schema=input_schema,
        output_schema=output_schema,
    )

    report = await ToolRegistryService(db_session, transport, manifest=manifest).refresh_service(
        DataTapService.SOCIAL_GROW
    )
    await db_session.refresh(row)

    assert "renamed-search-creators" in report.quarantined_remote_names
    assert row.review_status == "quarantined"
    assert row.is_enabled is False


def test_committed_manifest_contains_only_reviewed_datatap_tool() -> None:
    path = Path(__file__).parents[2] / "app" / "mcp_gateway" / "approved_tools.json"
    manifest = json.loads(path.read_text())

    assert manifest["manifest_version"] == 1
    assert [tool["internal_name"] for tool in manifest["tools"]] == [
        "datatap.xiaohongshu.kol.search.v1",
        "datatap.douyin.kol.search.v1",
    ]


async def test_manifest_disabled_tool_is_rejected_even_if_database_enabled(
    db_session,
) -> None:
    row, registry = await _registry_with_manifest_disabled_tool(db_session)

    with pytest.raises(ToolNotEnabledError):
        await registry.require_enabled(row.internal_tool_name)


async def test_manifest_disabled_tool_is_omitted_from_enabled_listing(db_session) -> None:
    _row, registry = await _registry_with_manifest_disabled_tool(db_session)

    assert await registry.list_enabled() == ()


async def test_refresh_forces_manifest_disabled_catalog_row_off(db_session) -> None:
    row, registry = await _registry_with_manifest_disabled_tool(db_session)

    report = await registry.refresh_service(DataTapService.SOCIAL_GROW)
    await db_session.refresh(row)

    assert report.approved_remote_names == ("search-creators",)
    assert row.review_status == "approved"
    assert row.is_enabled is False
