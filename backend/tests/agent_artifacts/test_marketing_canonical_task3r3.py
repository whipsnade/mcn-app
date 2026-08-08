"""Task 3R3：全 Builder 的 metric-level canonical contributor 回归。"""

from __future__ import annotations

from app.agent_artifacts.builders.brand import (
    _build_dimension_rows,
    _build_topics,
    build_brand_report_draft,
)
from app.agent_artifacts.builders.campaign import (
    _build_attribution,
    _build_comparisons,
    _build_content_types,
    _build_internal_metrics,
    _build_kol_contributions,
    _build_organic_summary,
    _build_overview,
    _build_platform_contributions,
    _build_roi,
)
from app.agent_artifacts.builders.common import LineageCollector
from app.agent_artifacts.builders.raw_rows import RowRef
from app.agent_artifacts.builders.sections import build_sentiment_section
from app.agent_artifacts.canonical import unit_for_path


def _ref(name: str, **row: object) -> RowRef:
    return RowRef(name, f"/{name}", dict(row))


def _evidence_ids(collector: LineageCollector, path: str) -> tuple[str, ...]:
    refs = {item["artifact_path"]: item["sources"] for item in collector.build()}
    return tuple(source["evidence_id"] for source in refs.get(path, []))


def test_campaign_comparison_keeps_each_metric_and_period_contributors_separate() -> None:
    current = [_ref("current-volume", volume=10), _ref("current-engagement", engagement=20)]
    baseline = [_ref("baseline-volume", volume=5), _ref("baseline-engagement", engagement=10)]
    collector = LineageCollector()

    _build_comparisons(current, baseline, [], collector)

    volume = "/data/comparisons/current_baseline/0"
    engagement = "/data/comparisons/current_baseline/1"
    assert _evidence_ids(collector, f"{volume}/current") == ("current-volume",)
    assert _evidence_ids(collector, f"{volume}/baseline") == ("baseline-volume",)
    assert _evidence_ids(collector, f"{volume}/delta") == ("current-volume", "baseline-volume")
    assert _evidence_ids(collector, f"{volume}/rate") == ("current-volume", "baseline-volume")
    assert _evidence_ids(collector, f"{engagement}/current") == ("current-engagement",)
    assert _evidence_ids(collector, f"{engagement}/baseline") == ("baseline-engagement",)


def test_campaign_comparison_posts_creators_and_explicit_zero_keep_real_rows() -> None:
    current = [_ref("post", author="甲", volume=0), _ref("no-author", volume=0)]
    baseline = [_ref("baseline-post", author="乙", volume=0)]
    collector = LineageCollector()

    comparisons, _ = _build_comparisons(current, baseline, [], collector)

    assert comparisons["current_baseline"][0]["current"] == 0
    assert _evidence_ids(collector, "/data/comparisons/current_baseline/2/current") == (
        "post",
        "no-author",
    )
    assert _evidence_ids(collector, "/data/comparisons/current_baseline/3/current") == ("post",)


def test_brand_topics_and_dimensions_use_only_metric_contributors() -> None:
    topic_rows = [_ref("topic-volume", topic="咖啡", volume=10), _ref("topic-eng", topic="咖啡", engagement=20)]
    collector = LineageCollector()
    _build_topics(topic_rows, collector)
    assert _evidence_ids(collector, "/data/topics/0/volume") == ("topic-volume",)
    assert _evidence_ids(collector, "/data/topics/0/engagement") == ("topic-eng",)

    dimension_rows = [_ref("label-only", platform="小红书", content_type="图文"), _ref("dimension-volume", platform="小红书", content_type="图文", volume=3)]
    collector = LineageCollector()
    _build_dimension_rows(dimension_rows, ("content_type",), "content_types", "type", (), collector)
    assert _evidence_ids(collector, "/data/content_types/0/volume") == ("dimension-volume",)


def test_campaign_creator_engagement_and_organic_metrics_exclude_non_contributors() -> None:
    rows = [
        _ref("author-engagement", platform="小红书", author="甲", engagement=20, is_paid="否", volume=3),
        _ref("author-no-engagement", platform="小红书", author="甲", is_paid="否"),
        _ref("no-author", platform="小红书", engagement=5),
    ]
    collector = LineageCollector()
    _build_overview(rows, None, [], collector)
    _build_platform_contributions(rows, collector)
    _build_kol_contributions(rows, collector)
    _build_organic_summary(rows, collector)
    assert _evidence_ids(collector, "/data/overview/total_creators") == ("author-engagement", "author-no-engagement")
    assert _evidence_ids(collector, "/data/platform_contributions/0/creators") == ("author-engagement", "author-no-engagement")
    assert _evidence_ids(collector, "/data/kol_contributions/0/engagement") == ("author-engagement",)
    assert _evidence_ids(collector, "/data/organic_summary/engagement") == ("author-engagement",)


def test_campaign_content_attribution_and_internal_formula_contributors_are_exact() -> None:
    collector = LineageCollector()
    _build_content_types([_ref("content-eng", platform="小红书", content_type="图文", engagement=9), _ref("content-no-eng", platform="小红书", content_type="图文")], collector)
    _build_attribution([_ref("paid", is_paid="是"), _ref("organic", is_paid="否")], collector)
    metrics, _ = _build_internal_metrics([
        _ref("spend", spend=100), _ref("impressions", impressions=1000), _ref("conversions", conversions=10), _ref("revenue", revenue=300)
    ], collector)
    assert _evidence_ids(collector, "/data/content_types/0/engagement") == ("content-eng",)
    assert _evidence_ids(collector, "/data/attribution/paid_confirmed") == ("paid",)
    assert _evidence_ids(collector, "/data/internal_metrics/spend") == ("spend",)
    assert _evidence_ids(collector, "/data/internal_metrics/cpc") == ("spend", "conversions")
    assert _evidence_ids(collector, "/data/internal_metrics/cpm") == ("spend", "impressions")
    assert _build_roi({"attribution_rules": ["7d"]}, metrics) is not None


def test_comparison_units_resolve_metric_from_data_item() -> None:
    brand_data = {"comparisons": {"mom": {"metrics": [{"metric": "total_volume"}, {"metric": "total_engagement"}, {"metric": "total_posts"}]}}}
    campaign_data = {"comparisons": {"current_baseline": [{"metric": "volume"}, {"metric": "engagement"}, {"metric": "posts"}, {"metric": "creators"}]}}
    assert unit_for_path("/data/comparisons/mom/metrics/0/current", module="brand", data=brand_data) == "mentions"
    assert unit_for_path("/data/comparisons/mom/metrics/1/delta", module="brand", data=brand_data) == "interactions"
    assert unit_for_path("/data/comparisons/mom/metrics/2/baseline", module="brand", data=brand_data) == "posts"
    assert unit_for_path("/data/comparisons/current_baseline/0/current", module="campaign", data=campaign_data) == "posts"
    assert unit_for_path("/data/comparisons/current_baseline/1/delta", module="campaign", data=campaign_data) == "interactions"
    assert unit_for_path("/data/comparisons/current_baseline/3/current", module="campaign", data=campaign_data) == "count"
    assert unit_for_path("/data/comparisons/current_baseline/3/rate", module="campaign", data=campaign_data) == "ratio"


def test_brand_fallback_sentiment_bucket_uses_only_its_component_rows() -> None:
    payload = build_brand_report_draft(
        scope={
            "brand": "测试品牌",
            "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
            "platforms": ["xiaohongshu"],
            "keywords": ["测试"],
            "comparison_mode": "none",
        },
        evidence={
            "overview_current": [
                ("positive", [{"平台": "小红书", "正面": 10}]),
                ("negative", [{"平台": "小红书", "负面": 2}]),
            ]
        },
    ).payload
    fields = {field.path: field for field in payload["canonical_data"]}

    assert fields["/data/sentiment/summary/positive/count"].evidence_ids == ("positive",)
    assert fields["/data/sentiment/summary/negative/count"].evidence_ids == ("negative",)
    assert fields["/data/sentiment/summary/positive/share"].evidence_ids == (
        "positive",
        "negative",
    )


def test_sentiment_share_uses_numerator_and_denominator_contributors() -> None:
    collector = LineageCollector()
    build_sentiment_section(
        [_ref("positive", sentiment="正面", volume=10), _ref("negative", sentiment="负面", volume=2)],
        collector,
    )

    assert _evidence_ids(collector, "/data/sentiment/summary/positive/count") == ("positive",)
    assert _evidence_ids(collector, "/data/sentiment/summary/positive/share") == (
        "positive",
        "negative",
    )
