"""Task 3R2：canonical publication 不变量与精确 Evidence contributor。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import build_campaign_report_draft
from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.canonical import CanonicalField, CanonicalPayloadMixin

_BRAND_SCOPE = {
    "brand": "测试品牌",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu", "douyin"],
    "keywords": ["测试"],
    "comparison_mode": "none",
}
_CAMPAIGN_SCOPE = {
    "brand": "测试品牌",
    "campaign": "测试活动",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu", "douyin"],
    "keywords": ["测试"],
}


class _CanonicalData(CanonicalPayloadMixin):
    data: dict[str, Any]


def _field(path: str, value: Any, *, evidence_ids: tuple[str, ...] = ()) -> CanonicalField:
    return CanonicalField(
        path=path,
        value=value,
        availability="complete",
        evidence_ids=evidence_ids,
    )


def _canonical_payload() -> dict[str, Any]:
    data = {
        "overview": {"total_volume": 1, "label/a~b": date(2026, 7, 1)},
        "series": ("one", "two"),
    }
    fields = (
        _field("/data/overview/total_volume", 1, evidence_ids=("ev-1",)),
        _field("/data/overview/label~1a~0b", date(2026, 7, 1)),
        _field("/data/series/0", "one"),
        _field("/data/series/1", "two"),
    )
    return {
        "data": data,
        "canonical_data": fields,
        "field_lineage": {field.path: (field.path,) for field in fields},
    }


def _by_path(payload: dict[str, Any]) -> dict[str, CanonicalField]:
    return {field.path: field for field in payload["canonical_data"]}


def _brand_evidence(overview_rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, Any]]]:
    return {
        "overview_current": [(f"ev-overview-{index}", [row]) for index, row in enumerate(overview_rows)],
        "sentiment": [
            ("ev-s-xhs", [{"平台": "小红书", "情感": "正面", "声量": 10}]),
            ("ev-s-dy", [{"平台": "抖音", "情感": "负面", "声量": 5}]),
        ],
        "daily_trend": [
            ("ev-trend", [{"日期": "2026-07-01", "平台": "小红书", "声量": 10, "互动数": 20}])
        ],
        "top_posts": [
            (
                "ev-top",
                [
                    {
                        "平台": "小红书",
                        "帖子ID": "top-1",
                        "标题": "热帖",
                        "作者": "作者",
                        "发布时间": "2026-07-01",
                        "帖子链接": "https://example.test/top-1",
                        "互动数": 20,
                    }
                ],
            )
        ],
    }


def _post(platform: str, post_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "平台": platform,
        "帖子ID": post_id,
        "标题": f"标题-{post_id}",
        "作者": f"作者-{post_id}",
        "发布时间": "2026-07-01 09:00:00",
        "帖子链接": f"https://example.test/{post_id}",
        "互动数": 10,
    }
    row.update(overrides)
    return row


def test_rejects_canonical_value_that_differs_from_final_data() -> None:
    payload = _canonical_payload()
    payload["canonical_data"] = tuple(
        field.model_copy(update={"value": 999})
        if field.path == "/data/overview/total_volume"
        else field
        for field in payload["canonical_data"]
    )

    with pytest.raises(ValidationError):
        _CanonicalData.model_validate(payload)


def test_rejects_missing_canonical_data_leaf_and_lineage() -> None:
    payload = _canonical_payload()
    path = "/data/overview/total_volume"
    payload["canonical_data"] = tuple(field for field in payload["canonical_data"] if field.path != path)
    payload["field_lineage"].pop(path)

    with pytest.raises(ValidationError):
        _CanonicalData.model_validate(payload)


def test_rejects_canonical_path_not_present_in_final_data() -> None:
    payload = _canonical_payload()
    extra = _field("/data/overview/not~1present", "unexpected")
    payload["canonical_data"] = (*payload["canonical_data"], extra)
    payload["field_lineage"][extra.path] = (extra.path,)

    with pytest.raises(ValidationError):
        _CanonicalData.model_validate(payload)


def test_wrapped_result_keeps_both_rows_and_sums_volume() -> None:
    rows = [
        {"平台": "小红书", "声量": 100, "互动数": 10, "发帖数": 1},
        {"平台": "抖音", "声量": 200, "互动数": 20, "发帖数": 2},
    ]
    evidence = _brand_evidence(rows)
    evidence["overview_current"] = [("ev-wrapped", {"result": json.dumps(rows, ensure_ascii=False)})]

    payload = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence).payload

    assert payload["data"]["overview"]["total_volume"] == 300


def test_wrapped_result_keeps_both_platform_contributions() -> None:
    rows = [
        {"平台": "小红书", "声量": 100, "互动数": 10, "发帖数": 1},
        {"平台": "抖音", "声量": 200, "互动数": 20, "发帖数": 2},
    ]
    evidence = _brand_evidence(rows)
    evidence["overview_current"] = [("ev-wrapped", {"result": json.dumps(rows, ensure_ascii=False)})]

    payload = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence).payload

    assert [item["platform"] for item in payload["data"]["overview"]["platforms"]] == [
        "xiaohongshu",
        "douyin",
    ]


def test_brand_overview_metric_lineage_uses_only_metric_contributors() -> None:
    evidence = _brand_evidence([])
    evidence["overview_current"] = [
        ("ev-volume", [{"平台": "小红书", "声量": 100}]),
        ("ev-engagement", [{"平台": "小红书", "互动数": 20}]),
    ]

    payload = build_brand_report_draft(
        scope={**_BRAND_SCOPE, "platforms": ["xiaohongshu"]}, evidence=evidence
    ).payload
    fields = _by_path(payload)

    assert fields["/data/overview/total_volume"].evidence_ids == ("ev-volume",)
    assert fields["/data/overview/total_engagement"].evidence_ids == ("ev-engagement",)


def test_brand_platform_sentiment_score_uses_its_own_platform_evidence() -> None:
    evidence = _brand_evidence(
        [
            {"平台": "小红书", "声量": 100},
            {"平台": "抖音", "声量": 200},
        ]
    )

    payload = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence).payload
    fields = _by_path(payload)

    assert fields["/data/overview/platforms/0/sentiment_score"].evidence_ids == ("ev-s-xhs",)
    assert fields["/data/overview/platforms/1/sentiment_score"].evidence_ids == ("ev-s-dy",)


def test_campaign_volume_units_are_posts() -> None:
    payload = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={"posts": [("ev-post", [_post("小红书", "p-1"), _post("抖音", "p-2")])]},
    ).payload
    fields = _by_path(payload)

    assert fields["/data/overview/total_volume"].unit == "posts"
    assert fields["/data/platform_contributions/0/volume"].unit == "posts"
    assert fields["/data/timeline/0/volume"].unit == "posts"


def test_brand_volume_unit_remains_mentions() -> None:
    payload = build_brand_report_draft(
        scope=_BRAND_SCOPE,
        evidence=_brand_evidence(
            [
                {"平台": "小红书", "声量": 100},
                {"平台": "抖音", "声量": 200},
            ]
        ),
    ).payload

    assert _by_path(payload)["/data/overview/total_volume"].unit == "mentions"


def test_brand_daily_trend_date_and_platform_keep_evidence_lineage() -> None:
    payload = build_brand_report_draft(
        scope={**_BRAND_SCOPE, "platforms": ["xiaohongshu"]},
        evidence=_brand_evidence([{"平台": "小红书", "声量": 100}]),
    ).payload
    fields = _by_path(payload)

    assert fields["/data/daily_trend/0/date"].evidence_ids == ("ev-trend",)
    assert fields["/data/daily_trend/0/platform"].evidence_ids == ("ev-trend",)


def test_brand_daily_trend_metric_lineage_uses_only_metric_contributors() -> None:
    evidence = _brand_evidence([])
    evidence["daily_trend"] = [
        ("ev-daily-volume", [{"日期": "2026-07-01", "平台": "小红书", "声量": 10}]),
        ("ev-daily-engagement", [{"日期": "2026-07-01", "平台": "小红书", "互动数": 20}]),
    ]

    payload = build_brand_report_draft(
        scope={**_BRAND_SCOPE, "platforms": ["xiaohongshu"]}, evidence=evidence
    ).payload
    fields = _by_path(payload)

    assert fields["/data/daily_trend/0/volume"].evidence_ids == ("ev-daily-volume",)
    assert fields["/data/daily_trend/0/engagement"].evidence_ids == ("ev-daily-engagement",)


def test_campaign_timeline_date_and_platform_keep_evidence_lineage() -> None:
    payload = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-post", [_post("小红书", "p-1")])]},
    ).payload
    fields = _by_path(payload)

    assert fields["/data/timeline/0/date"].evidence_ids == ("ev-post",)
    assert fields["/data/timeline/0/platform"].evidence_ids == ("ev-post",)


def test_top_post_without_platform_is_skipped_and_disclosed() -> None:
    payload = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-post", [_post("小红书", "p-1", 平台=None)])]},
    ).payload

    assert payload["data"]["top_posts"] == []
    assert payload["availability"]["top_posts"]["status"] == "partial"
    assert any(item["code"] == "post_platform_missing" for item in payload["limitations"])


@pytest.mark.parametrize("missing,code", [("标题", "post_title_missing"), ("作者", "post_author_missing")])
def test_top_post_missing_text_field_is_partial_even_with_url(missing: str, code: str) -> None:
    payload = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-post", [_post("小红书", "p-1", **{missing: None})])]},
    ).payload
    fields = _by_path(payload)
    field_name = "title" if missing == "标题" else "author"

    assert fields[f"/data/top_posts/0/{field_name}"].availability == "unavailable"
    assert payload["availability"]["top_posts"]["status"] == "partial"
    assert any(item["code"] == code for item in payload["limitations"])


def test_brand_platform_coverage_marks_each_affected_section_partial() -> None:
    evidence = _brand_evidence([{"平台": "小红书", "声量": 100, "互动数": 20, "发帖数": 1}])
    evidence["sentiment"] = [
        ("ev-s-xhs", [{"平台": "小红书", "情感": "正面", "声量": 10}])
    ]
    payload = build_brand_report_draft(
        scope=_BRAND_SCOPE,
        evidence=evidence,
    ).payload

    for section in ("overview", "sentiment", "daily_trend", "top_posts"):
        assert payload["availability"][section]["status"] == "partial"
    # 已观测平台的原始趋势值仍 complete；partial 只影响跨平台完整性判断。
    assert _by_path(payload)["/data/daily_trend/0/volume"].availability == "complete"


def test_campaign_platform_coverage_marks_each_affected_section_partial() -> None:
    payload = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={"posts": [("ev-post", [_post("小红书", "p-1", 情感="正面")])]},
    ).payload

    for section in ("overview", "platform_contributions", "sentiment", "timeline", "top_posts"):
        assert payload["availability"][section]["status"] == "partial"
    assert _by_path(payload)["/data/platform_contributions/0/share"].availability == "partial"


def test_social_metric_conflict_only_marks_same_metric_canonical_field_partial() -> None:
    payload = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={
            "posts": [("ev-post", [_post("小红书", "p-1", 声量=10, 互动数=10)])],
            "upload": [("ev-upload", [{"平台": "小红书", "声量": 20, "互动数": 30}])],
        },
    ).payload
    fields = _by_path(payload)

    # 声量与活动帖子数不是同一 canonical 口径；冲突只做 limitation 披露。
    assert fields["/data/overview/total_volume"].availability == "complete"
    assert fields["/data/overview/total_engagement"].availability == "partial"


def test_brand_rejects_evidence_reused_across_exclusive_period_groups() -> None:
    evidence = _brand_evidence([{"平台": "小红书", "声量": 100}])
    evidence["overview_mom"] = [("ev-overview-0", [{"平台": "小红书", "声量": 50}])]

    with pytest.raises(DraftBuildError, match="evidence_period_reuse"):
        build_brand_report_draft(scope={**_BRAND_SCOPE, "comparison_mode": "mom"}, evidence=evidence)


def test_campaign_rejects_evidence_reused_across_exclusive_period_groups() -> None:
    row = _post("小红书", "p-1")
    evidence = {
        "current": [("ev-period", [row])],
        "baseline": [("ev-period", [row])],
        "post": [("ev-period", [row])],
    }

    with pytest.raises(DraftBuildError, match="evidence_period_reuse"):
        build_campaign_report_draft(scope=_CAMPAIGN_SCOPE, evidence=evidence)


def test_compatible_posts_social_reuse_is_counted_once() -> None:
    row = _post("小红书", "p-1")
    payload = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-post", [row])], "social": [("ev-post", [row])]},
    ).payload

    assert payload["data"]["overview"]["total_volume"] == 1
    assert _by_path(payload)["/data/overview/total_volume"].evidence_ids == ("ev-post",)


def test_canonical_output_and_evidence_id_order_remain_stable() -> None:
    evidence = _brand_evidence(
        [
            {"平台": "小红书", "声量": 100, "互动数": 20},
            {"平台": "抖音", "声量": 200, "互动数": 30},
        ]
    )
    first = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence).payload
    second = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence).payload

    assert [field.model_dump(mode="json") for field in first["canonical_data"]] == [
        field.model_dump(mode="json") for field in second["canonical_data"]
    ]
    assert _by_path(first)["/data/overview/total_volume"].evidence_ids == (
        "ev-overview-0",
        "ev-overview-1",
    )
