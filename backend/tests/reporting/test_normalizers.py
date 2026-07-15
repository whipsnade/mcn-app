import pytest

from app.reporting.normalizers import UnknownEvidenceToolError, normalize_tool_evidence
from tests.reporting.fakes import evidence


def test_same_nickname_on_different_platform_accounts_is_not_merged() -> None:
    rows = normalize_tool_evidence(
        [
            evidence(platform="bilibili", account_id="100", nickname="同名达人"),
            evidence(platform="bilibili", account_id="200", nickname="同名达人"),
        ]
    )

    assert [(row.platform, row.platform_account_id) for row in rows] == [
        ("bilibili", "100"),
        ("bilibili", "200"),
    ]


def test_missing_metrics_stay_none_and_are_reported() -> None:
    row = normalize_tool_evidence([evidence(platform="bilibili", account_id="100")])[0]

    assert row.engagement_rate is None
    assert "engagement_rate" in row.missing_fields


def test_currency_quote_is_normalized_to_cny_before_persistence() -> None:
    row = normalize_tool_evidence([evidence(quoted_price_cny="￥1.2万")])[0]

    assert row.quoted_price_cny == 12_000


def test_unknown_internal_tool_is_rejected_without_guessing_fields() -> None:
    item = evidence()
    item = item.__class__(
        internal_tool_name="unreviewed.tool.v1",
        payload=item.payload,
        source_call_id=item.source_call_id,
        collected_at=item.collected_at,
    )

    with pytest.raises(UnknownEvidenceToolError):
        normalize_tool_evidence([item])
