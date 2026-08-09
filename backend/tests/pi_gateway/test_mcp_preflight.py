from types import SimpleNamespace

from app.pi_gateway.accounting import McpPermit
from app.pi_gateway.service import _adapter_catalog_entry


def test_mcp_permit_has_fixed_cost_and_no_caller_supplied_amount() -> None:
    permit = McpPermit(
        permit_id="permit-1",
        tenant_id="tenant-1",
        user_id="user-1",
        run_id="run-1",
        tool_call_id="call-1",
        catalog_entry_id="catalog-1",
    )
    assert permit.amount == 10
    assert "amount" not in permit.model_fields_set


def test_adapter_catalog_uses_discovered_remote_name_and_wire_service() -> None:
    row = SimpleNamespace(
        id="catalog-1",
        internal_tool_name="query_analysis_data",
        service_slug="insight-cube-mcp",
        discovery_digest="a" * 64,
    )
    entry = _adapter_catalog_entry(row, "query_analysis_data.remote")
    assert entry.service == "insight-cube-mcp"
    assert entry.adapter_visible_name == "query_analysis_data"
    assert entry.remote_name == "query_analysis_data.remote"
