import pytest
from datetime import UTC, datetime

from app.reporting.normalizers import UnknownEvidenceToolError, normalize_tool_evidence
from app.reporting.schemas import ToolEvidence
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


def test_risk_flag_evidence_is_recursively_redacted() -> None:
    row = normalize_tool_evidence(
        [
            evidence(
                risk_flags=[
                    {
                        "token": "should-not-persist",
                        "nested": {
                            "endpoint": "https://datatap.deepminer.com.cn/api",
                            "reason": "内容重复",
                        },
                    }
                ]
            )
        ]
    )[0]

    assert row.risk_flags == ({"nested": {"reason": "内容重复"}},)


def test_risk_flags_redact_normalized_key_variants_and_secret_text() -> None:
    row = normalize_tool_evidence(
        [
            evidence(
                risk_flags=[
                    {
                        "api-key": "secret-api-key",
                        "API＿Key": "unicode-secret-key",
                        "secret": "secret-value",
                        "AuthorizationHeader": "secret-auth",
                        "safe": "内容稳定",
                        "note": "  Bearer private-value",
                    },
                    {
                        "note": "api　key： private-value",
                        "token_note": "token private-value",
                        "credentialNote": "credential private-value",
                        "safe": "互动正常",
                    },
                ]
            )
        ]
    )[0]

    assert row.risk_flags == ({"safe": "内容稳定"}, {"safe": "互动正常"})


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


def test_datatap_xiaohongshu_result_is_normalized_from_reviewed_chinese_fields() -> None:
    item = ToolEvidence(
        internal_tool_name="datatap.xiaohongshu.kol.search.v1",
        payload={
            "result": (
                '{"KOL 列表":[{"账号ID (kwUid)":"kol-1","平台":"xiaohongshu",'
                '"昵称":"护肤达人","主页":"https://example.test/kol-1","粉丝数":831811,'
                '"有效粉丝率":0.618,"综合评分":94.48,"互动率-视频笔记":0.0723,'
                '"预估报价-视频":57277,"近30天是否有发文":true,"是否活跃":true}]}'
            )
        },
        source_call_id="call-1",
        collected_at=datetime.now(UTC).replace(tzinfo=None),
    )

    row = normalize_tool_evidence([item])[0]

    assert row.platform == "xiaohongshu"
    assert row.platform_account_id == "kol-1"
    assert row.followers == 831811
    assert row.engagement_rate == 7.23
    assert row.quoted_price_cny == 57277
    assert row.content_score == 94.48
    assert row.audience_score == 61.8
    assert row.engagement_score == 7.23
    assert row.risk_flags == ()
