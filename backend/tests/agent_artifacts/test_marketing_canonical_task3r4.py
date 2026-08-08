"""Task 3R4：历史 canonical 兼容、活动平台口径与数值单位门禁。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_artifacts.builders.campaign import build_campaign_report_draft
from app.agent_artifacts.canonical import unit_for_path
from app.agent_artifacts.exporters import export_artifact
from app.agent_artifacts.payloads import BrandReportV3, CampaignReportV2
from app.agent_artifacts.validation import ArtifactPayloadInvalid, ArtifactPayloadValidator
from tests.agent_artifacts.test_payloads import build_brand_dict, build_campaign_dict


class _Version:
    def __init__(self, schema_version: str, payload_json: dict[str, Any]) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json


def _legacy_payload(builder: Any) -> dict[str, Any]:
    payload = deepcopy(builder())
    payload.pop("canonical_data", None)
    payload.pop("field_lineage", None)
    return payload


@pytest.mark.parametrize(
    ("model", "builder", "schema_version"),
    [
        (BrandReportV3, build_brand_dict, "brand_report_v3"),
        (CampaignReportV2, build_campaign_dict, "campaign_report_v2"),
    ],
)
def test_legacy_brand_and_campaign_payloads_are_readable_and_exportable(
    model: Any, builder: Any, schema_version: str
) -> None:
    payloads = [_legacy_payload(builder)]
    payloads.append({**deepcopy(payloads[0]), "canonical_data": [], "field_lineage": {}})

    for payload in payloads:
        model.model_validate(payload)
        workbook = export_artifact(_Version(schema_version, payload))
        assert workbook[:2] == b"PK"


@pytest.mark.parametrize(
    ("model", "builder"),
    [(BrandReportV3, build_brand_dict), (CampaignReportV2, build_campaign_dict)],
)
def test_canonical_fields_must_be_paired_for_payload_reads(model: Any, builder: Any) -> None:
    payload = _legacy_payload(builder)
    canonical = builder()["canonical_data"]
    payload["canonical_data"] = canonical[:1]

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("module", "schema_version", "business_fields", "builder"),
    [
        ("brand", "brand_report_v3", {"brand": "某品牌"}, build_brand_dict),
        (
            "campaign",
            "campaign_report_v2",
            {"brand": "某品牌", "campaign": "C1"},
            build_campaign_dict,
        ),
    ],
)
def test_new_and_updated_payloads_require_complete_canonical_contract(
    module: str,
    schema_version: str,
    business_fields: dict[str, str],
    builder: Any,
) -> None:
    payload = _legacy_payload(builder)

    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module=module,
            schema_version=schema_version,
            artifact_type=schema_version,
            business_fields=business_fields,
            payload=payload,
        )
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_revision_payload(
            module=module,
            schema_version=schema_version,
            artifact_type=schema_version,
            payload=payload,
        )


def _campaign_post(post_id: str, platform: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "帖子ID": post_id,
        "标题": f"标题-{post_id}",
        "作者": f"作者-{post_id}",
        "发布时间": "2026-07-01 09:00:00",
        "互动数": 10,
    }
    if platform is not None:
        row["平台"] = platform
    return row


def test_campaign_missing_platform_posts_are_excluded_from_platform_sections() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu", "douyin"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [_campaign_post("missing-platform", None), _campaign_post("xhs", "小红书")],
                )
            ]
        },
    ).payload

    assert payload["data"]["overview"]["total_volume"] == 1
    assert payload["data"]["overview"]["total_posts"] == 1
    assert all(item["platform"] != "all" for item in payload["data"]["platform_contributions"])
    assert all(item["platform"] != "all" for item in payload["data"]["timeline"])
    for section in ("overview", "platform_contributions", "timeline"):
        assert payload["availability"][section]["status"] == "partial"
    assert any(item["code"] == "post_platform_missing" for item in payload["limitations"])


def test_campaign_missing_platform_metric_row_is_disclosed_without_overdegrading() -> None:
    """纯指标行也必须被识别，但不能影响没有使用该行的章节。"""
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [_campaign_post("xhs", "小红书"), {"互动数": 99}],
                )
            ]
        },
    ).payload

    fields = {field.path: field for field in payload["canonical_data"]}
    assert payload["data"]["overview"]["total_engagement"] == 10
    assert payload["availability"]["overview"]["status"] == "partial"
    assert payload["availability"]["platform_contributions"]["status"] == "partial"
    assert payload["availability"]["timeline"]["status"] == "complete"
    assert fields["/data/overview/total_volume"].availability == "complete"
    assert fields["/data/overview/total_posts"].availability == "complete"
    assert fields["/data/overview/total_engagement"].availability == "partial"
    assert fields["/data/platform_contributions/0/volume"].availability == "complete"
    assert fields["/data/platform_contributions/0/posts"].availability == "complete"
    assert fields["/data/platform_contributions/0/share"].availability == "complete"
    assert any(item["code"] == "post_platform_missing" for item in payload["limitations"])


def test_campaign_explicit_aggregate_platform_is_not_treated_as_missing() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [_campaign_post("xhs", "小红书"), {"平台": "合计", "互动数": 99}],
                )
            ]
        },
    ).payload

    assert not any(item["code"] == "post_platform_missing" for item in payload["limitations"])
    assert any(item["platform"] == "all" for item in payload["data"]["platform_contributions"])


def test_campaign_missing_platform_metric_marks_only_comparison_metric() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-current",
                    [_campaign_post("current", "小红书"), {"互动数": 99}],
                )
            ],
            "baseline": [
                (
                    "ev-baseline",
                    [{**_campaign_post("baseline", "小红书"), "互动数": 5}],
                )
            ],
        },
    ).payload

    fields = {field.path: field for field in payload["canonical_data"]}
    for period in ("current", "baseline", "delta", "rate"):
        assert fields[f"/data/comparisons/current_baseline/1/{period}"].availability == "partial"
        assert fields[f"/data/comparisons/current_baseline/2/{period}"].availability == "complete"


def test_social_volume_outside_scope_period_is_not_compared() -> None:
    evidence = {
        "posts": [
            (
                "ev-posts",
                [{**_campaign_post("p1", "小红书"), "声量": 10}],
            )
        ],
        "upload": [
            (
                "ev-upload",
                [{"平台": "合计", "声量": 20, "日期": "2026-01-15"}],
            )
        ],
    }
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence=evidence,
    ).payload
    baseline = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={"posts": evidence["posts"]},
    ).payload

    fields = {field.path: field for field in payload["canonical_data"]}
    assert not any(item["code"] == "social_metric_conflict" for item in payload["limitations"])
    assert payload["data_status"] == baseline["data_status"]
    assert payload["availability"]["overview"] == baseline["availability"]["overview"]
    assert fields["/data/overview/total_volume"].availability == "complete"


def test_same_period_social_volume_conflict_only_discloses_source_metric() -> None:
    evidence = {
        "posts": [
            (
                "ev-posts",
                [{**_campaign_post("p1", "小红书"), "声量": 10}],
            )
        ],
        "upload": [
            (
                "ev-upload",
                [{"平台": "合计", "声量": 20, "日期": "2026-07-15"}],
            )
        ],
    }
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence=evidence,
    ).payload
    baseline = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={"posts": evidence["posts"]},
    ).payload

    fields = {field.path: field for field in payload["canonical_data"]}
    conflicts = [item for item in payload["limitations"] if item["code"] == "social_metric_conflict"]
    assert conflicts
    assert all(item["affected_paths"] == [] for item in conflicts)
    assert payload["data_status"] == baseline["data_status"]
    assert payload["availability"]["overview"] == baseline["availability"]["overview"]
    assert fields["/data/overview/total_volume"].availability == "complete"
    assert not any("internal_metrics.spend" in item["affected_paths"] for item in conflicts)


def test_social_volume_unit_mismatch_is_not_compared() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [{**_campaign_post("p1", "小红书"), "声量": 10, "单位": "mentions"}],
                )
            ],
            "upload": [
                (
                    "ev-upload",
                    [{"平台": "合计", "声量": 20, "单位": "posts", "日期": "2026-07-15"}],
                )
            ],
        },
    ).payload

    assert not any(item["code"] == "social_metric_conflict" for item in payload["limitations"])


def test_social_engagement_unit_mismatch_is_not_compared() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [{**_campaign_post("p1", "小红书"), "单位": "likes"}],
                )
            ],
            "upload": [
                (
                    "ev-upload",
                    [{"平台": "合计", "互动数": 20, "单位": "shares", "日期": "2026-07-15"}],
                )
            ],
        },
    ).payload

    assert not any(item["code"] == "social_metric_conflict" for item in payload["limitations"])


def test_social_volume_conflict_is_disclosed_without_mislabeling_post_or_spend_fields() -> None:
    payload = build_campaign_report_draft(
        scope={
            "brand": "测试品牌",
            "campaign": "测试活动",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [{"平台": "小红书", "帖子ID": "p1", "声量": 10, "互动数": 10, "日期": "2026-07-15"}],
                )
            ],
            "upload": [
                (
                    "ev-upload",
                    [{"平台": "合计", "声量": 20, "互动数": 30, "日期": "2026-07-15"}],
                )
            ],
        },
    ).payload

    fields = {field.path: field for field in payload["canonical_data"]}
    assert fields["/data/overview/total_volume"].availability == "complete"
    assert fields["/data/overview/total_engagement"].availability == "partial"
    conflicts = [item for item in payload["limitations"] if item["code"] == "social_metric_conflict"]
    assert conflicts
    affected = [path for item in conflicts for path in item["affected_paths"]]
    assert "/data/overview/total_volume" not in affected
    assert "overview.total_volume" not in affected
    assert "internal_metrics.spend" not in affected


def test_numeric_canonical_fields_have_units_and_formula_units_are_stable() -> None:
    for payload in (build_brand_dict(), build_campaign_dict()):
        for field in payload["canonical_data"]:
            if isinstance(field["value"], (int, float)) and not isinstance(field["value"], bool):
                assert field["unit"] is not None, field["path"]

    assert unit_for_path("/data/overview/total_creators", module="campaign") == "count"
    assert unit_for_path("/data/overview/sentiment_score", module="campaign") == "score"
    assert unit_for_path("/data/internal_metrics/cpc", module="campaign") == "currency_per_conversion"
    assert unit_for_path(
        "/data/internal_metrics/cpm", module="campaign"
    ) == "currency_per_thousand_impressions"
    assert unit_for_path("/data/roi/roi", module="campaign") == "ratio"
    assert unit_for_path("/data/roi/roas", module="campaign") == "ratio"
