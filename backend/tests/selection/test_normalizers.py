import pytest

from app.selection.normalizers import UnknownEvidenceToolError, normalize_tool_evidence
from tests.selection.fakes import evidence


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


def _datatap_kol_list_evidence(tool: str, rows: list[dict]):
    import json
    from datetime import UTC, datetime

    from app.selection.schemas import ToolEvidence

    return ToolEvidence(
        internal_tool_name=tool,
        payload={"result": json.dumps({"KOL 列表": rows}, ensure_ascii=False)},
        source_call_id=None,
        collected_at=datetime.now(UTC).replace(tzinfo=None),
    )


def test_xiaohongshu_adapter_skips_row_missing_identity() -> None:
    rows = normalize_tool_evidence(
        [
            _datatap_kol_list_evidence(
                "datatap.xiaohongshu.kol.search.v1",
                [
                    {"昵称": "缺ID达人"},  # 缺 账号ID (kwUid)，单行失败
                    {"账号ID (kwUid)": "xhs-1", "昵称": "正常达人", "综合评分": 88},
                ],
            )
        ]
    )

    assert [row.platform_account_id for row in rows] == ["xhs-1"]


def test_xiaohongshu_adapter_skips_row_with_out_of_range_score() -> None:
    rows = normalize_tool_evidence(
        [
            _datatap_kol_list_evidence(
                "datatap.xiaohongshu.kol.search.v1",
                [
                    {"账号ID (kwUid)": "xhs-bad", "综合评分": 120},  # 评分越界
                    {"账号ID (kwUid)": "xhs-good", "综合评分": 60},
                ],
            )
        ]
    )

    assert [row.platform_account_id for row in rows] == ["xhs-good"]


def test_douyin_adapter_skips_bad_row_and_keeps_valid_rows() -> None:
    rows = normalize_tool_evidence(
        [
            _datatap_kol_list_evidence(
                "datatap.douyin.kol.search.v1",
                [
                    {"昵称": "缺ID达人"},
                    {"账号ID (kwUid)": "dy-1", "昵称": "抖音达人"},
                ],
            )
        ]
    )

    assert [row.platform_account_id for row in rows] == ["dy-1"]
