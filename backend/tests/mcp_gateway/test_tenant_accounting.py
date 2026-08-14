import pytest
from pydantic import ValidationError

from app.pi_gateway.accounting import McpPreflightContext


def test_mcp_preflight_context_cannot_accept_gateway_price_or_identity_fields() -> None:
    with pytest.raises(ValidationError):
        McpPreflightContext.model_validate(
            {
                "tenant_id": "tenant-1",
                "user_id": "user-1",
                "run_id": "run-1",
                "tool_call_id": "call-1",
                "internal_tool_name": "query_analysis_data",
                "service_slug": "insight-cube-mcp",
                "arguments": {},
                "feature": "brand_analysis",
                "amount": 1,
                "remote_name": "unreviewed-tool",
            }
        )
