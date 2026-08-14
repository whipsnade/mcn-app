"""Pi production path must not contain the historical Evidence bridge."""

from __future__ import annotations

import inspect

from app.pi_gateway import service as pi_gateway_service
from app.pi_gateway.service import PiGatewayService


def test_pi_finalize_is_metadata_only_and_has_no_evidence_bridge_call_sites() -> None:
    source = inspect.getsource(PiGatewayService.finalize_mcp)
    assert "parse_mcp_result_details" not in source
    assert "EvidenceWriter" not in source
    assert "validate_output" not in source
    assert "settle_mcp_call_metadata" in source


def test_pi_gateway_module_does_not_import_legacy_evidence_result_parser() -> None:
    assert not hasattr(pi_gateway_service, "parse_mcp_result_details")
    assert not hasattr(pi_gateway_service, "EvidenceWriter")
