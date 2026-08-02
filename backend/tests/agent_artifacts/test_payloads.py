"""Contract tests for the five strongly-typed artifact payloads + insight_board_v1.

Field-level definitions are authoritative in the design spec §12.1/§12.2.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.agent_artifacts.payloads import (
    TYPED_PAYLOAD_BY_SCHEMA,
    BrandReportV3,
    CampaignReportV2,
    InsightBoardV1,
    KolAnalysisV2,
    KolDetailV2,
    KolSelectionV3,
)

DT = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
PERIOD = {"start": date(2026, 1, 1), "end": date(2026, 1, 31), "timezone": "Asia/Shanghai"}

EIGHT_DIMENSIONS = (
    "industry_interest",
    "target_region",
    "target_age",
    "engagement",
    "active_follower",
    "content",
    "followers",
    "engagement_follower_ratio",
)
WEIGHTS = {
    "industry_interest": 10,
    "target_region": 8,
    "target_age": 8,
    "engagement": 20,
    "active_follower": 15,
    "content": 15,
    "followers": 10,
    "engagement_follower_ratio": 14,
}

COMPLETE = {"status": "complete", "reason_codes": []}


def _bucket() -> dict:
    return {"count": 10, "share": 0.5}


def _sentiment() -> dict:
    return {
        "summary": {
            "positive": _bucket(),
            "neutral": _bucket(),
            "negative": _bucket(),
        },
        "by_platform": [
            {
                "platform": "xiaohongshu",
                "positive": _bucket(),
                "neutral": _bucket(),
                "negative": _bucket(),
            }
        ],
    }


def _top_post(post_id: str = "p1") -> dict:
    return {
        "platform": "xiaohongshu",
        "post_id": post_id,
        "title": "热帖",
        "url": "https://x.com/a",
        "author": "author",
        "published_at": DT,
        "likes": 100,
        "comments": 10,
        "shares": 5,
        "engagement": 115,
    }


def _snapshot() -> dict:
    return {
        "version": "kol_score_v2",
        "total": 78.0,
        "rating": "重点推荐",
        "stars": "★★★★★",
        "data_completeness": 100.0,
        "dimensions": {
            dim: {
                "raw_score": 80.0,
                "weight": WEIGHTS[dim],
                "weighted_score": round(80.0 * WEIGHTS[dim] / 100, 2),
                "source": "evidence:score_inputs",
                "missing_reason": None,
            }
            for dim in EIGHT_DIMENSIONS
        },
    }


def build_brand_dict() -> dict:
    return {
        "schema_version": "brand_report_v3",
        "module": "brand",
        "scope": {
            "brand": "某品牌",
            "period": PERIOD,
            "platforms": ["xiaohongshu"],
            "keywords": ["咖啡"],
            "comparison_mode": "mom",
        },
        "data_status": "complete",
        "availability": {
            s: COMPLETE
            for s in (
                "overview",
                "sentiment",
                "daily_trend",
                "topics",
                "top_posts",
                "comparisons",
                "content_types",
                "creator_tiers",
                "organic_vs_paid",
                "regions",
            )
        },
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "data": {
            "overview": {
                "total_volume": 1000,
                "total_engagement": 5000,
                "total_posts": 50,
                "sentiment_score": 0.6,
                "platforms": [
                    {
                        "platform": "xiaohongshu",
                        "volume": 1000,
                        "engagement": 5000,
                        "posts": 50,
                        "share_of_voice": 1.0,
                        "sentiment_score": 0.6,
                    }
                ],
            },
            "comparisons": {
                "mom": {
                    "status": "complete",
                    "baseline_period": PERIOD,
                    "metrics": [
                        {
                            "metric": "total_volume",
                            "current": 1000,
                            "baseline": 800,
                            "delta": 200,
                            "rate": 0.25,
                        }
                    ],
                },
                "yoy": {"status": "not_requested", "baseline_period": None, "metrics": []},
            },
            "sentiment": _sentiment(),
            "daily_trend": [
                {
                    "date": date(2026, 1, 1),
                    "platform": "xiaohongshu",
                    "volume": 100,
                    "engagement": 500,
                    "positive": 70,
                    "neutral": 20,
                    "negative": 10,
                }
            ],
            "content_types": [
                {"platform": "xiaohongshu", "type": "图文", "posts": 30, "volume": 600, "engagement": 3000}
            ],
            "creator_tiers": [],
            "organic_vs_paid": [],
            "regions": [{"region": "上海", "volume": 300, "share": 0.3, "sentiment_score": 0.7}],
            "topics": [{"topic": "咖啡", "volume": 500, "engagement": 2500, "sentiment_score": 0.6}],
            "top_posts": [_top_post()],
        },
        "narrative": {
            "executive_summary": "整体向好",
            "findings": [
                {"title": "总量增长", "detail": "细节", "supporting_paths": ["overview.total_volume"]}
            ],
            "recommendations": [],
        },
    }


def build_campaign_dict() -> dict:
    return {
        "schema_version": "campaign_report_v2",
        "module": "campaign",
        "scope": {
            "brand": "某品牌",
            "campaign": "C1",
            "period": PERIOD,
            "platforms": ["xiaohongshu"],
            "keywords": [],
        },
        "data_status": "complete",
        "availability": {
            s: COMPLETE
            for s in (
                "overview",
                "platform_contributions",
                "timeline",
                "sentiment",
                "top_posts",
                "kol_contributions",
                "content_types",
            )
        },
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "data": {
            "overview": {
                "total_volume": 1000,
                "total_engagement": 5000,
                "total_posts": 50,
                "total_creators": 5,
                "sentiment_score": 0.6,
            },
            "platform_contributions": [
                {
                    "platform": "xiaohongshu",
                    "volume": 1000,
                    "engagement": 5000,
                    "posts": 50,
                    "creators": 5,
                    "share": 1.0,
                }
            ],
            "timeline": [
                {
                    "date": date(2026, 1, 1),
                    "platform": "xiaohongshu",
                    "volume": 100,
                    "engagement": 500,
                    "posts": 5,
                }
            ],
            "kol_contributions": [
                {
                    "platform": "xiaohongshu",
                    "kol_uid": "k1",
                    "nickname": "达人",
                    "posts": 5,
                    "volume": 100,
                    "engagement": 500,
                    "contribution_share": 0.5,
                }
            ],
            "content_types": [
                {"platform": "xiaohongshu", "type": "图文", "posts": 30, "volume": 600, "engagement": 3000}
            ],
            "sentiment": _sentiment(),
            "top_posts": [_top_post()],
        },
        "narrative": {
            "executive_summary": "活动整体积极",
            "phase_review": [
                {"phase": "预热", "detail": "细节", "supporting_paths": ["overview.total_volume"]}
            ],
            "findings": [],
            "recommendations": [],
        },
    }


def build_kol_selection_dict() -> dict:
    return {
        "schema_version": "kol_selection_v3",
        "module": "kol",
        "scope": {
            "brand": "某品牌",
            "category": None,
            "campaign": None,
            "platforms": ["xiaohongshu"],
            "audience": {"regions": ["上海"], "age_ranges": ["25-34"], "interests": ["咖啡"]},
            "filters": {
                "budget_min": None,
                "budget_max": None,
                "follower_min": None,
                "follower_max": None,
            },
        },
        "data_status": "complete",
        "availability": {
            s: COMPLETE for s in ("scoring", "items", "summary")
        },
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "data": {
            "scoring": {
                "version": "kol_score_v2",
                "method": "weighted_sum",
                "weights": WEIGHTS,
                "missing_value_policy": "missing_as_zero",
            },
            "items": [
                {
                    "rank": 1,
                    "platform": "xiaohongshu",
                    "kol_uid": "k1",
                    "nickname": "达人",
                    "avatar_url": None,
                    "homepage_url": "https://x.com/k1",
                    "followers": 100000,
                    "active_followers": 50000,
                    "active_follower_rate": 50.0,
                    "growth_rate": 2.0,
                    "engagement_total": 5000,
                    "avg_engagement": 50.0,
                    "likes": 3000,
                    "comments": 1000,
                    "shares": 1000,
                    "quoted_price": 1000,
                    "reasons": ["互动高"],
                    "missing_fields": [],
                    "audience": {"regions": ["上海"], "age_ranges": ["25-34"], "interests": ["咖啡"]},
                    "score_snapshot": _snapshot(),
                }
            ],
            "summary": {
                "candidate_count": 10,
                "selected_count": 5,
                "platform_distribution": [{"key": "xhs", "label": "小红书", "count": 5, "share": 1.0}],
                "rating_distribution": [{"key": "A", "label": "重点推荐", "count": 3, "share": 0.6}],
            },
        },
        "narrative": {
            "selection_summary": "已圈选 5 位达人",
            "fit_findings": [
                {
                    "text": "互动高",
                    "kol_uid": "k1",
                    "supporting_paths": ["items.0.score_snapshot.total"],
                }
            ],
            "risk_notes": [],
            "usage_advice": [],
        },
    }


def build_kol_analysis_dict() -> dict:
    return {
        "schema_version": "kol_analysis_v2",
        "module": "kol",
        "scope": {"selection_artifact_id": "art-sel", "selection_version": "1", "analysis_period": None},
        "data_status": "complete",
        "availability": {
            s: COMPLETE
            for s in (
                "summary",
                "kol_trend",
                "top_kols",
                "platform_distribution",
                "rating_distribution",
                "follower_distribution",
                "engagement_distribution",
                "region_distribution",
            )
        },
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "data": {
            "summary": {
                "kol_count": 5,
                "total_followers": 500000,
                "total_active_followers": 250000,
                "total_engagement": 25000,
                "avg_score": 78.0,
            },
            "platform_distribution": [{"key": "xhs", "label": "小红书", "count": 5, "share": 1.0}],
            "rating_distribution": [{"key": "A", "label": "重点推荐", "count": 3, "share": 0.6}],
            "follower_distribution": [{"key": "100k", "label": "10万+", "count": 3, "share": 0.6}],
            "engagement_distribution": [{"key": "high", "label": "高互动", "count": 2, "share": 0.4}],
            "region_distribution": [{"key": "sh", "label": "上海", "count": 3, "share": 0.6}],
            "kol_trend": [
                {
                    "platform": "xiaohongshu",
                    "kol_uid": "k1",
                    "nickname": "达人",
                    "followers": 100000,
                    "active_followers": 50000,
                    "engagement_total": 5000,
                    "avg_engagement": 50.0,
                    "growth_rate": 2.0,
                    "score": 78.0,
                }
            ],
            "top_kols": [
                {
                    "rank": 1,
                    "platform": "xiaohongshu",
                    "kol_uid": "k1",
                    "nickname": "达人",
                    "score": 78.0,
                    "engagement_total": 5000,
                    "rating": "重点推荐",
                }
            ],
        },
        "narrative": {
            "executive_summary": "组合健康",
            "portfolio_findings": [
                {"title": "头部门户", "detail": "细节", "supporting_paths": ["summary.kol_count"]}
            ],
            "mix_recommendations": [],
            "risk_notes": [],
        },
    }


def build_kol_detail_dict() -> dict:
    return {
        "schema_version": "kol_detail_v2",
        "module": "kol",
        "scope": {
            "platform": "xiaohongshu",
            "kol_uid": "k1",
            "selection_artifact_id": None,
            "selection_version": None,
        },
        "data_status": "complete",
        "availability": {
            s: COMPLETE for s in ("identity", "metrics", "audience", "trend", "latest_posts", "cache")
        },
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "data": {
            "identity": {
                "nickname": "达人",
                "avatar_url": "https://x.com/a.png",
                "homepage_url": "https://x.com/k1",
                "bio": "简介",
                "verification": True,
                "region": "上海",
            },
            "metrics": {
                "followers": 100000,
                "following": 100,
                "posts": 500,
                "likes": 50000,
                "active_followers": 50000,
                "active_follower_rate": 50.0,
                "growth_rate": 2.0,
                "engagement_total": 5000,
                "avg_engagement": 50.0,
            },
            "audience": {
                "gender_distribution": [{"key": "f", "label": "女", "value": 60000, "share": 0.6}],
                "age_distribution": [{"key": "25-34", "label": "25-34", "value": 40000, "share": 0.4}],
                "region_distribution": [{"key": "sh", "label": "上海", "value": 30000, "share": 0.3}],
                "interest_distribution": [{"key": "coffee", "label": "咖啡", "value": 20000, "share": 0.2}],
            },
            "trend": [
                {"date": date(2026, 1, 1), "followers": 100000, "engagement": 500, "posts": 5}
            ],
            "latest_posts": [
                {
                    "post_id": "p1",
                    "title": "热帖",
                    "url": "https://x.com/p1",
                    "published_at": DT,
                    "likes": 100,
                    "comments": 10,
                    "shares": 5,
                    "engagement": 115,
                }
            ],
            "cache": {"hit": True, "fetched_at": DT, "expires_at": DT},
        },
        "narrative": {
            "profile_summary": "达人性价比高",
            "content_strengths": [
                {"title": "内容强", "detail": "细节", "supporting_paths": ["metrics.followers"]}
            ],
            "commercial_notes": [],
            "risk_notes": [],
        },
    }


def build_insight_dict() -> dict:
    return {
        "schema_version": "insight_board_v1",
        "module": "brand",
        "title": "品牌钻取",
        "scope": {
            "summary": "围绕品牌概览",
            "period": PERIOD,
            "platforms": ["xiaohongshu"],
            "brand": "某品牌",
            "campaign": None,
            "kol_uid": None,
        },
        "parent_artifact_id": "art-1",
        "data_status": "complete",
        "availability": {"blocks": COMPLETE},
        "limitations": [],
        "methodology": {"data_as_of": DT, "source_names": ["DataTap"], "notes": []},
        "narrative": {"summary": "摘要", "findings": []},
        "data": [
            {
                "block_type": "metric_grid",
                "title": "概览",
                "cards": [{"key": "volume", "label": "声量", "value": 1000}],
            },
            {"block_type": "markdown", "title": "说明", "content": "文字"},
        ],
    }


BUILDERS: dict = {
    BrandReportV3: build_brand_dict,
    CampaignReportV2: build_campaign_dict,
    KolSelectionV3: build_kol_selection_dict,
    KolAnalysisV2: build_kol_analysis_dict,
    KolDetailV2: build_kol_detail_dict,
    InsightBoardV1: build_insight_dict,
}

PAYLOAD_CASES = [
    (BrandReportV3, "brand_report_v3", "brand"),
    (CampaignReportV2, "campaign_report_v2", "campaign"),
    (KolSelectionV3, "kol_selection_v3", "kol"),
    (KolAnalysisV2, "kol_analysis_v2", "kol"),
    (KolDetailV2, "kol_detail_v2", "kol"),
]


# ---------------------------------------------------------------- schema_version


def test_type_map_keys_are_fixed() -> None:
    assert set(TYPED_PAYLOAD_BY_SCHEMA) == {
        "brand_report_v3",
        "campaign_report_v2",
        "kol_selection_v3",
        "kol_analysis_v2",
        "kol_detail_v2",
        "insight_board_v1",
    }
    assert TYPED_PAYLOAD_BY_SCHEMA["brand_report_v3"] is BrandReportV3
    assert TYPED_PAYLOAD_BY_SCHEMA["insight_board_v1"] is InsightBoardV1


@pytest.mark.parametrize(("model", "version"), [(m, v) for m, v, _ in PAYLOAD_CASES])
def test_schema_version_is_fixed_literal(model: type, version: str) -> None:
    d = BUILDERS[model]()
    d["schema_version"] = "not-" + version
    with pytest.raises(ValidationError):
        model.model_validate(d)
    inst = model.model_validate(BUILDERS[model]())
    assert inst.schema_version == version


def test_insight_schema_version_is_fixed_literal() -> None:
    d = build_insight_dict()
    d["schema_version"] = "insight_board_v9"
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d)
    assert InsightBoardV1.model_validate(build_insight_dict()).schema_version == "insight_board_v1"


# ---------------------------------------------------------------- extra=forbid


@pytest.mark.parametrize(("model",), [(m,) for m, _, _ in PAYLOAD_CASES])
def test_extra_forbid_top_level(model: type) -> None:
    d = BUILDERS[model]()
    d["unknown_top_level"] = True
    with pytest.raises(ValidationError):
        model.model_validate(d)


@pytest.mark.parametrize(
    ("model", "nested_key"),
    [
        (BrandReportV3, "overview"),
        (CampaignReportV2, "overview"),
        (KolSelectionV3, "summary"),
        (KolAnalysisV2, "summary"),
        (KolDetailV2, "metrics"),
    ],
)
def test_extra_forbid_nested_data(model: type, nested_key: str) -> None:
    d = BUILDERS[model]()
    d["data"][nested_key]["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(d)


def test_extra_forbid_insight_board() -> None:
    d = build_insight_dict()
    d["data"][0]["unexpected_block_field"] = 1
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d)


# ---------------------------------------------------------------- length caps


def test_brand_top_posts_capped_at_20() -> None:
    d = build_brand_dict()
    d["data"]["top_posts"] = [_top_post(f"p{i}") for i in range(21)]
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)
    ok = build_brand_dict()
    ok["data"]["top_posts"] = [_top_post(f"p{i}") for i in range(20)]
    assert BrandReportV3.model_validate(ok).data.top_posts[19].post_id == "p19"


def test_campaign_top_posts_capped_at_20() -> None:
    d = build_campaign_dict()
    d["data"]["top_posts"] = [_top_post(f"p{i}") for i in range(21)]
    with pytest.raises(ValidationError):
        CampaignReportV2.model_validate(d)


def test_kol_selection_items_capped_at_20() -> None:
    d = build_kol_selection_dict()
    item = d["data"]["items"][0]
    d["data"]["items"] = [item.copy() for _ in range(21)]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)


def test_kol_analysis_lists_capped_at_20() -> None:
    for key in ("kol_trend", "top_kols"):
        d = build_kol_analysis_dict()
        row = d["data"][key][0]
        d["data"][key] = [row.copy() for _ in range(21)]
        with pytest.raises(ValidationError):
            KolAnalysisV2.model_validate(d)


def test_kol_detail_latest_posts_capped_at_5() -> None:
    d = build_kol_detail_dict()
    post = d["data"]["latest_posts"][0]
    d["data"]["latest_posts"] = [post.copy() for _ in range(6)]
    with pytest.raises(ValidationError):
        KolDetailV2.model_validate(d)
    ok = build_kol_detail_dict()
    ok["data"]["latest_posts"] = [post.copy() for _ in range(5)]
    assert len(KolDetailV2.model_validate(ok).data.latest_posts) == 5


def test_insight_blocks_and_cards_bounded() -> None:
    d = build_insight_dict()
    d["data"] = [d["data"][1]] * 51  # 51 blocks
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d)

    d2 = build_insight_dict()
    card = d2["data"][0]["cards"][0]
    d2["data"][0]["cards"] = [card.copy() for _ in range(17)]  # 17 cards
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d2)


# ---------------------------------------------------------------- url protocol


@pytest.mark.parametrize(
    "bad",
    ["javascript:alert(1)", "data:text/html;base64,xx", "ftp://example.com/a", "example.com/no-scheme"],
)
def test_brand_top_post_url_rejects_bad_schemes(bad: str) -> None:
    d = build_brand_dict()
    d["data"]["top_posts"][0]["url"] = bad
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


@pytest.mark.parametrize("good", ["https://x.com/a", "http://x.com/b"])
def test_brand_top_post_url_accepts_http_https(good: str) -> None:
    d = build_brand_dict()
    d["data"]["top_posts"][0]["url"] = good
    assert BrandReportV3.model_validate(d).data.top_posts[0].url == good


def test_campaign_top_post_url_rejects_bad_schemes() -> None:
    d = build_campaign_dict()
    d["data"]["top_posts"][0]["url"] = "ftp://example.com/a"
    with pytest.raises(ValidationError):
        CampaignReportV2.model_validate(d)


def test_kol_selection_homepage_url_rejects_bad_schemes() -> None:
    d = build_kol_selection_dict()
    d["data"]["items"][0]["homepage_url"] = "javascript:void(0)"
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)
    d2 = build_kol_selection_dict()
    d2["data"]["items"][0]["homepage_url"] = "https://x.com/k1"
    assert KolSelectionV3.model_validate(d2).data.items[0].homepage_url == "https://x.com/k1"


def test_kol_detail_urls_reject_bad_schemes() -> None:
    d = build_kol_detail_dict()
    d["data"]["identity"]["homepage_url"] = "ftp://example.com/k1"
    with pytest.raises(ValidationError):
        KolDetailV2.model_validate(d)
    d2 = build_kol_detail_dict()
    d2["data"]["latest_posts"][0]["url"] = "javascript:void(0)"
    with pytest.raises(ValidationError):
        KolDetailV2.model_validate(d2)


# ---------------------------------------------------------------- null numeric -> limitation


def test_brand_null_numeric_rejected_when_section_complete() -> None:
    d = build_brand_dict()
    d["data"]["overview"]["total_volume"] = None
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


def test_brand_null_numeric_allowed_with_partial_and_limitation() -> None:
    d = build_brand_dict()
    d["availability"]["overview"] = {"status": "partial", "reason_codes": ["data_partial"]}
    d["data_status"] = "restricted"
    d["limitations"] = [
        {
            "code": "L_VOLUME",
            "message": "声量数据部分缺失",
            "affected_paths": ["overview.total_volume"],
        }
    ]
    d["data"]["overview"]["total_volume"] = None
    inst = BrandReportV3.model_validate(d)
    # null must survive: never coerced to 0
    assert inst.data.overview.total_volume is None


def test_brand_null_numeric_with_partial_but_no_limitation_rejected() -> None:
    d = build_brand_dict()
    d["availability"]["overview"] = {"status": "partial", "reason_codes": ["data_partial"]}
    d["data_status"] = "restricted"
    d["limitations"] = []
    d["data"]["overview"]["total_volume"] = None
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


# ---------------------------------------------------------------- data_status aggregation


REQUIRED_SECTION_BY_MODEL = {
    BrandReportV3: "overview",
    CampaignReportV2: "overview",
    KolSelectionV3: "summary",
    KolAnalysisV2: "summary",
    KolDetailV2: "metrics",
    InsightBoardV1: "blocks",
}


@pytest.mark.parametrize("model", list(REQUIRED_SECTION_BY_MODEL))
def test_aggregation_all_complete_accepts_complete(model: type) -> None:
    inst = model.model_validate(BUILDERS[model]())
    assert inst.data_status == "complete"


@pytest.mark.parametrize("model", list(REQUIRED_SECTION_BY_MODEL))
def test_aggregation_partial_section_rejects_complete(model: type) -> None:
    d = BUILDERS[model]()
    section = REQUIRED_SECTION_BY_MODEL[model]
    d["availability"][section] = {"status": "partial", "reason_codes": ["missing"]}
    d["data_status"] = "complete"
    with pytest.raises(ValidationError):
        model.model_validate(d)


@pytest.mark.parametrize("model", list(REQUIRED_SECTION_BY_MODEL))
def test_aggregation_partial_section_requires_restricted_and_limitation(model: type) -> None:
    section = REQUIRED_SECTION_BY_MODEL[model]
    d = BUILDERS[model]()
    d["availability"][section] = {"status": "partial", "reason_codes": ["missing"]}
    d["data_status"] = "restricted"
    d["limitations"] = [
        {"code": "L_PARTIAL", "message": "部分数据缺失", "affected_paths": [section]}
    ]
    inst = model.model_validate(d)
    assert inst.data_status == "restricted"

    d2 = BUILDERS[model]()
    d2["availability"][section] = {"status": "partial", "reason_codes": ["missing"]}
    d2["data_status"] = "restricted"
    d2["limitations"] = []
    with pytest.raises(ValidationError):
        model.model_validate(d2)


# ---------------------------------------------------------------- narrative supporting_paths


def test_narrative_supporting_path_must_resolve_in_data() -> None:
    d = build_brand_dict()
    d["narrative"]["findings"][0]["supporting_paths"] = ["nonexistent.section.field"]
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)
    ok = build_brand_dict()
    ok["narrative"]["recommendations"] = [
        {"title": "建议", "action": "a", "rationale": "r", "supporting_paths": ["overview.total_engagement"]}
    ]
    assert BrandReportV3.model_validate(ok)


def test_kol_selection_narrative_supporting_path_must_resolve() -> None:
    d = build_kol_selection_dict()
    d["narrative"]["fit_findings"][0]["supporting_paths"] = ["items.0.no_such_field"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)


# ---------------------------------------------------------------- kol_selection_v3 scoring


def test_scoring_version_must_be_kol_score_v2() -> None:
    d = build_kol_selection_dict()
    d["data"]["scoring"]["version"] = "kol_score_v3"
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)


def test_scoring_method_must_be_weighted_sum() -> None:
    d = build_kol_selection_dict()
    d["data"]["scoring"]["method"] = "rank_sum"
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)


def test_scoring_weights_must_match_exactly() -> None:
    assert sum(WEIGHTS.values()) == 100

    bad = build_kol_selection_dict()
    bad["data"]["scoring"]["weights"] = dict(WEIGHTS)
    bad["data"]["scoring"]["weights"]["industry_interest"] = 9
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(bad)

    missing = build_kol_selection_dict()
    missing["data"]["scoring"]["weights"] = dict(WEIGHTS)
    del missing["data"]["scoring"]["weights"]["content"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(missing)

    extra = build_kol_selection_dict()
    extra["data"]["scoring"]["weights"] = dict(WEIGHTS)
    extra["data"]["scoring"]["weights"]["bonus"] = 1
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(extra)


def test_score_snapshot_requires_eight_dimensions() -> None:
    d = build_kol_selection_dict()
    snap = d["data"]["items"][0]["score_snapshot"]
    assert set(snap["dimensions"]) == set(EIGHT_DIMENSIONS)

    drop = build_kol_selection_dict()
    del drop["data"]["items"][0]["score_snapshot"]["dimensions"]["content"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(drop)

    extra_dim = build_kol_selection_dict()
    extra_dim["data"]["items"][0]["score_snapshot"]["dimensions"]["bonus"] = {
        "raw_score": 80.0,
        "weight": 5,
        "weighted_score": 4.0,
        "source": None,
        "missing_reason": None,
    }
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(extra_dim)


def test_score_snapshot_dimension_requires_all_subfields() -> None:
    no_source = build_kol_selection_dict()
    del no_source["data"]["items"][0]["score_snapshot"]["dimensions"]["content"]["source"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(no_source)

    no_raw = build_kol_selection_dict()
    del no_raw["data"]["items"][0]["score_snapshot"]["dimensions"]["content"]["raw_score"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(no_raw)

    extra_field = build_kol_selection_dict()
    extra_field["data"]["items"][0]["score_snapshot"]["dimensions"]["content"]["bogus"] = 1
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(extra_field)


def test_score_snapshot_missing_version_or_total_rejected() -> None:
    d = build_kol_selection_dict()
    del d["data"]["items"][0]["score_snapshot"]["version"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)
    d2 = build_kol_selection_dict()
    del d2["data"]["items"][0]["score_snapshot"]["total"]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d2)


def test_valid_score_snapshot_passes() -> None:
    inst = KolSelectionV3.model_validate(build_kol_selection_dict())
    snap = inst.data.items[0].score_snapshot
    assert snap.version == "kol_score_v2"
    assert set(snap.dimensions) == set(EIGHT_DIMENSIONS)


# ---------------------------------------------------------------- insight_board_v1


def test_insight_blocks_whitelist() -> None:
    allowed = {
        "metric_grid",
        "table",
        "bar_chart",
        "line_chart",
        "pie_chart",
        "markdown",
        "timeline",
        "references",
    }
    d = build_insight_dict()
    d["data"][0]["block_type"] = "gauge"
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d)
    assert allowed == {
        "metric_grid",
        "table",
        "bar_chart",
        "line_chart",
        "pie_chart",
        "markdown",
        "timeline",
        "references",
    }


def test_insight_requires_module_title_scope_parent() -> None:
    for key in ("module", "title", "scope", "parent_artifact_id"):
        d = build_insight_dict()
        del d[key]
        with pytest.raises(ValidationError):
            InsightBoardV1.model_validate(d)


# ---------------------------------------------------------------- stable-key uniqueness


def test_kol_selection_items_duplicate_kol_uid_rejected() -> None:
    d = build_kol_selection_dict()
    item = d["data"]["items"][0]
    dup = item.copy()
    dup["rank"] = 2  # same platform + kol_uid
    d["data"]["items"] = [item, dup]
    with pytest.raises(ValidationError):
        KolSelectionV3.model_validate(d)


def test_kol_selection_items_distinct_pass() -> None:
    d = build_kol_selection_dict()
    item = d["data"]["items"][0]
    other = item.copy()
    other["rank"] = 2
    other["kol_uid"] = "k2"
    d["data"]["items"] = [item, other]
    assert len(KolSelectionV3.model_validate(d).data.items) == 2


def test_brand_top_posts_duplicate_post_id_rejected() -> None:
    d = build_brand_dict()
    d["data"]["top_posts"] = [_top_post("p1"), _top_post("p1")]
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


def test_brand_top_posts_distinct_pass() -> None:
    d = build_brand_dict()
    d["data"]["top_posts"] = [_top_post("p1"), _top_post("p2")]
    assert len(BrandReportV3.model_validate(d).data.top_posts) == 2


def test_campaign_top_posts_duplicate_post_id_rejected() -> None:
    d = build_campaign_dict()
    d["data"]["top_posts"] = [_top_post("p1"), _top_post("p1")]
    with pytest.raises(ValidationError):
        CampaignReportV2.model_validate(d)


def test_campaign_kol_contributions_duplicate_kol_uid_rejected() -> None:
    d = build_campaign_dict()
    row = d["data"]["kol_contributions"][0]
    d["data"]["kol_contributions"] = [row, row.copy()]
    with pytest.raises(ValidationError):
        CampaignReportV2.model_validate(d)


def test_brand_section_arrays_duplicate_keys_rejected() -> None:
    d = build_brand_dict()
    d["data"]["regions"] = [
        {"region": "上海", "volume": 300, "share": 0.3, "sentiment_score": 0.7}
    ] * 2
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)

    d2 = build_brand_dict()
    d2["data"]["topics"] = [
        {"topic": "咖啡", "volume": 500, "engagement": 2500, "sentiment_score": 0.6}
    ] * 2
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d2)


def test_kol_analysis_distribution_duplicate_key_rejected() -> None:
    d = build_kol_analysis_dict()
    d["data"]["platform_distribution"] = [{"key": "xhs", "label": "小红书", "count": 5, "share": 1.0}] * 2
    with pytest.raises(ValidationError):
        KolAnalysisV2.model_validate(d)


def test_kol_detail_audience_duplicate_key_rejected() -> None:
    d = build_kol_detail_dict()
    d["data"]["audience"]["gender_distribution"] = [
        {"key": "f", "label": "女", "value": 60000, "share": 0.6}
    ] * 2
    with pytest.raises(ValidationError):
        KolDetailV2.model_validate(d)


def test_insight_table_duplicate_columns_rejected() -> None:
    d = build_insight_dict()
    d["data"].append(
        {
            "block_type": "table",
            "title": "表",
            "columns": ["col_a", "col_b", "col_a"],
            "rows": [["1", "2", "3"]],
        }
    )
    with pytest.raises(ValidationError):
        InsightBoardV1.model_validate(d)


# ---------------------------------------------------------------- brand comparison invariant


def test_brand_comparison_not_requested_must_have_no_metrics() -> None:
    d = build_brand_dict()
    d["data"]["comparisons"]["yoy"]["status"] = "not_requested"
    d["data"]["comparisons"]["yoy"]["metrics"] = [
        {"metric": "total_volume", "current": 1000, "baseline": 800, "delta": 200, "rate": 0.25}
    ]
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


def test_brand_comparison_present_requires_metrics() -> None:
    d = build_brand_dict()
    d["data"]["comparisons"]["mom"]["status"] = "complete"
    d["data"]["comparisons"]["mom"]["metrics"] = []
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


def test_brand_comparison_not_requested_without_metrics_passes() -> None:
    inst = BrandReportV3.model_validate(build_brand_dict())
    assert inst.data.comparisons.yoy.status == "not_requested"
    assert inst.data.comparisons.yoy.metrics == ()
    assert len(inst.data.comparisons.mom.metrics) == 1
