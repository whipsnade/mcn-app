"""Deterministic local B7 topology checks; no model/MCP/network calls."""

from app.pi_gateway.events import normalize_source_payload, parse_source_event_id
from app.tenancy.service import effective_runtime_backend


def test_two_tenant_fake_topology_keeps_backend_and_event_identity_isolated() -> None:
    tenants = {"tenant-a": "pi", "tenant-b": "current"}
    assert effective_runtime_backend(tenants["tenant-a"], kill_switch=False) == "pi"
    assert effective_runtime_backend(tenants["tenant-b"], kill_switch=True) == "current"
    assert parse_source_event_id("attempt-a:1") != parse_source_event_id("attempt-b:1")
    assert normalize_source_payload("tool.start", {"call_id": "a", "internal_tool_name": "load_marketing_skill"}) == {
        "call_id": "a",
        "internal_tool_name": "load_marketing_skill",
    }
