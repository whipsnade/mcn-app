from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.registry import ToolRegistryService, discovery_digest
from app.mcp_gateway.transport import DiscoveredTool

from tests.mcp_gateway.fakes import strict_object_schema


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


def test_committed_manifest_is_deny_by_default() -> None:
    path = Path(__file__).parents[2] / "app" / "mcp_gateway" / "approved_tools.json"
    assert json.loads(path.read_text()) == {"manifest_version": 1, "tools": []}
