import pytest

from app.pi_gateway.accounting import TenantAccountingService


def test_accounting_classification_is_closed_set() -> None:
    with pytest.raises(ValueError, match="mcp_failure_classification_invalid"):
        TenantAccountingService.validate_failure_classification("retry_later")
