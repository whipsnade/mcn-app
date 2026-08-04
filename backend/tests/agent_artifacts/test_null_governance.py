"""Schema 驱动的递归 null 治理契约测试（v3 加固 §6.3 / Plan B 任务 B1）。

取代 ``SECTION_NUMERIC_PATHS`` 手工枚举：每个强类型 payload 声明受治理的
业务章节根（``GOVERNED_SECTIONS``，data 顶层字段名），校验器从 Pydantic
模型递归遍历该章节下**所有 Optional 数值叶子**（含数组元素内的叶子），
发现 null 时要求对应章节 availability 为 partial/unavailable 且有覆盖
limitation。豁免（§12.1 Lineage 与消费边界）：日期、枚举、稳定身份、版本、
展示顺序、纯文本标签、运行时元数据（缓存 TTL、schema version）不要求治理。

``SentimentBucket.count/share`` 可空：情感 unavailable 时允许 null +
limitation，不得伪造 0；真实零值是数值叶子，发布时必须带 lineage。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_artifacts.lineage import required_numeric_pointers
from app.agent_artifacts.payloads import (
    BrandReportV3,
    CampaignReportV2,
    KolAnalysisV2,
    KolDetailV2,
    KolSelectionV3,
)

from tests.agent_artifacts.test_payloads import (
    build_brand_dict,
    build_campaign_dict,
    build_kol_analysis_dict,
    build_kol_detail_dict,
    build_kol_selection_dict,
)
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft

PARTIAL = {"status": "partial", "reason_codes": ["data_partial"]}


def _limitation(*paths: str) -> dict:
    return {"code": "L_PARTIAL", "message": "部分数据缺失", "affected_paths": list(paths)}


def _restrict(d: dict, section: str, *limitation_paths: str) -> dict:
    """把章节标 partial、data_status 标 restricted 并挂 limitation。"""
    d["availability"][section] = PARTIAL
    d["data_status"] = "restricted"
    d["limitations"] = [_limitation(*limitation_paths)]
    return d


# ---------------------------------------------------------------- 声明契约


def test_section_numeric_paths_removed() -> None:
    """手工枚举清单已废除（§6.3）。"""
    for model in (BrandReportV3, CampaignReportV2, KolSelectionV3, KolAnalysisV2, KolDetailV2):
        assert not hasattr(model, "SECTION_NUMERIC_PATHS"), model.__name__


def test_governed_sections_are_known_data_fields() -> None:
    expected = {
        BrandReportV3: {
            "overview",
            "comparisons",
            "sentiment",
            "daily_trend",
            "content_types",
            "creator_tiers",
            "organic_vs_paid",
            "regions",
            "topics",
            "top_posts",
        },
        CampaignReportV2: {
            "overview",
            "platform_contributions",
            "timeline",
            "kol_contributions",
            "content_types",
            "sentiment",
            "top_posts",
        },
        KolSelectionV3: {"scoring", "items", "summary"},
        KolAnalysisV2: {
            "summary",
            "platform_distribution",
            "rating_distribution",
            "follower_distribution",
            "engagement_distribution",
            "region_distribution",
            "kol_trend",
            "top_kols",
        },
        # cache 是运行时元数据（hit/fetched_at/expires_at），不要求治理。
        KolDetailV2: {"identity", "metrics", "audience", "trend", "latest_posts"},
    }
    for model, sections in expected.items():
        assert set(model.GOVERNED_SECTIONS) == sections, model.__name__
        data_fields = set(model.model_fields["data"].annotation.model_fields)
        assert set(model.GOVERNED_SECTIONS) <= data_fields, model.__name__


# ---------------------------------------------------------------- 数组内 null 叶子被捕获


@pytest.mark.parametrize(
    ("build", "model", "section", "set_null"),
    [
        (
            build_brand_dict,
            BrandReportV3,
            "daily_trend",
            lambda d: d["data"]["daily_trend"][0].update(volume=None),
        ),
        (
            build_brand_dict,
            BrandReportV3,
            "topics",
            lambda d: d["data"]["topics"][0].update(volume=None),
        ),
        (
            build_brand_dict,
            BrandReportV3,
            "top_posts",
            lambda d: d["data"]["top_posts"][0].update(likes=None),
        ),
        (
            build_brand_dict,
            BrandReportV3,
            "overview",
            lambda d: d["data"]["overview"]["platforms"][0].update(volume=None),
        ),
        (
            build_brand_dict,
            BrandReportV3,
            "comparisons",
            lambda d: d["data"]["comparisons"]["mom"]["metrics"][0].update(rate=None),
        ),
        (
            build_campaign_dict,
            CampaignReportV2,
            "timeline",
            lambda d: d["data"]["timeline"][0].update(volume=None),
        ),
        (
            build_campaign_dict,
            CampaignReportV2,
            "kol_contributions",
            lambda d: d["data"]["kol_contributions"][0].update(share=None),
        ),
        (
            build_campaign_dict,
            CampaignReportV2,
            "top_posts",
            lambda d: d["data"]["top_posts"][0].update(engagement=None),
        ),
        (
            build_kol_selection_dict,
            KolSelectionV3,
            "items",
            lambda d: d["data"]["items"][0].update(followers=None),
        ),
        (
            build_kol_selection_dict,
            KolSelectionV3,
            "items",
            lambda d: d["data"]["items"][0].update(quoted_price=None),
        ),
        (
            build_kol_analysis_dict,
            KolAnalysisV2,
            "kol_trend",
            lambda d: d["data"]["kol_trend"][0].update(avg_engagement=None),
        ),
        (
            build_kol_analysis_dict,
            KolAnalysisV2,
            "top_kols",
            lambda d: d["data"]["top_kols"][0].update(score=None),
        ),
        (
            build_kol_detail_dict,
            KolDetailV2,
            "audience",
            lambda d: d["data"]["audience"]["gender_distribution"][0].update(value=None),
        ),
        (
            build_kol_detail_dict,
            KolDetailV2,
            "trend",
            lambda d: d["data"]["trend"][0].update(followers=None),
        ),
        (
            build_kol_detail_dict,
            KolDetailV2,
            "latest_posts",
            lambda d: d["data"]["latest_posts"][0].update(likes=None),
        ),
    ],
)
def test_array_null_leaf_rejected_when_section_complete(build, model, section, set_null) -> None:
    d = build()
    set_null(d)
    with pytest.raises(ValidationError):
        model.model_validate(d)


def test_brand_daily_trend_null_allowed_with_partial_and_section_limitation() -> None:
    d = build_brand_dict()
    d["data"]["daily_trend"][0]["volume"] = None
    _restrict(d, "daily_trend", "daily_trend")  # 章节级 limitation 覆盖数组内 null
    inst = BrandReportV3.model_validate(d)
    assert inst.data.daily_trend[0].volume is None  # null 不得被当 0


def test_brand_daily_trend_null_allowed_with_exact_path_limitation() -> None:
    d = build_brand_dict()
    d["data"]["daily_trend"][0]["engagement"] = None
    _restrict(d, "daily_trend", "daily_trend.0.engagement")
    inst = BrandReportV3.model_validate(d)
    assert inst.data.daily_trend[0].engagement is None


def test_kol_detail_trend_null_with_partial_but_no_limitation_rejected() -> None:
    d = build_kol_detail_dict()
    d["data"]["trend"][0]["followers"] = None
    d["availability"]["trend"] = PARTIAL
    d["data_status"] = "restricted"
    d["limitations"] = []
    with pytest.raises(ValidationError):
        KolDetailV2.model_validate(d)


def test_kol_selection_item_null_allowed_with_partial_and_limitation() -> None:
    d = build_kol_selection_dict()
    d["data"]["items"][0]["quoted_price"] = None
    _restrict(d, "items", "items.0.quoted_price")
    inst = KolSelectionV3.model_validate(d)
    assert inst.data.items[0].quoted_price is None


# ---------------------------------------------------------------- 豁免：可空非数值不受治理


def test_optional_non_numeric_leaves_are_exempt() -> None:
    """可空文本/URL/日期/外键（avatar_url、baseline_period、selection_artifact_id
    等）在 complete 章节下允许为 None——豁免清单不要求治理。"""
    d = build_kol_selection_dict()
    d["data"]["items"][0]["avatar_url"] = None
    d["data"]["items"][0]["homepage_url"] = None
    d["scope"]["brand"] = None
    inst = KolSelectionV3.model_validate(d)
    assert inst.data.items[0].avatar_url is None

    brand = build_brand_dict()
    assert brand["data"]["comparisons"]["yoy"]["baseline_period"] is None
    BrandReportV3.model_validate(brand)

    detail = build_kol_detail_dict()
    assert detail["scope"]["selection_artifact_id"] is None
    KolDetailV2.model_validate(detail)


# ---------------------------------------------------------------- SentimentBucket 可空


def _null_sentiment(d: dict) -> dict:
    d["data"]["sentiment"]["summary"]["positive"] = {"count": None, "share": None}
    return d


@pytest.mark.parametrize(
    ("build", "model"),
    [(build_brand_dict, BrandReportV3), (build_campaign_dict, CampaignReportV2)],
)
def test_sentiment_null_rejected_when_section_complete(build, model) -> None:
    d = _null_sentiment(build())
    with pytest.raises(ValidationError):
        model.model_validate(d)


@pytest.mark.parametrize(
    ("build", "model"),
    [(build_brand_dict, BrandReportV3), (build_campaign_dict, CampaignReportV2)],
)
def test_sentiment_null_allowed_with_partial_and_limitation(build, model) -> None:
    d = _null_sentiment(build())
    _restrict(d, "sentiment", "sentiment")
    inst = model.model_validate(d)
    assert inst.data.sentiment.summary.positive.count is None
    assert inst.data.sentiment.summary.positive.share is None


def test_sentiment_null_with_partial_but_no_limitation_rejected() -> None:
    d = _null_sentiment(build_brand_dict())
    d["availability"]["sentiment"] = PARTIAL
    d["data_status"] = "restricted"
    d["limitations"] = []
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)


def test_sentiment_by_platform_null_also_governed() -> None:
    d = build_brand_dict()
    d["data"]["sentiment"]["by_platform"][0]["negative"] = {"count": None, "share": None}
    with pytest.raises(ValidationError):
        BrandReportV3.model_validate(d)
    _restrict(d, "sentiment", "sentiment")
    inst = BrandReportV3.model_validate(d)
    assert inst.data.sentiment.by_platform[0].negative.count is None


# ---------------------------------------------------------------- 真实零值：lineage 边界


def test_sentiment_real_zero_requires_lineage_pointer() -> None:
    """真实零值是数值叶子：发布边界要求 lineage（required_numeric_pointers 命中）；
    null 叶子不产生 lineage 指针（由 partial + limitation 治理），两者不冲突。"""
    zeroed = build_brand_dict()
    zeroed["data"]["sentiment"]["summary"]["negative"] = {"count": 0, "share": 0.0}
    payload = BrandReportV3.model_validate(zeroed).model_dump(mode="json")
    pointers = required_numeric_pointers(payload)
    assert "/data/sentiment/summary/negative/count" in pointers
    assert "/data/sentiment/summary/negative/share" in pointers

    nulled = _null_sentiment(build_brand_dict())
    _restrict(nulled, "sentiment", "sentiment")
    payload = BrandReportV3.model_validate(nulled).model_dump(mode="json")
    pointers = required_numeric_pointers(payload)
    assert "/data/sentiment/summary/positive/count" not in pointers
    assert "/data/sentiment/summary/positive/share" not in pointers


# ---------------------------------------------------------------- 完整 payload 不回归


def test_complete_payloads_still_validate() -> None:
    for build, model in (
        (build_brand_dict, BrandReportV3),
        (build_campaign_dict, CampaignReportV2),
        (build_kol_selection_dict, KolSelectionV3),
        (build_kol_analysis_dict, KolAnalysisV2),
        (build_kol_detail_dict, KolDetailV2),
    ):
        inst = model.model_validate(build())
        assert inst.data_status == "complete"


# ---------------------------------------------------------------- builder 稀疏数据适配
#
# §6.3 起数组元素内的 Optional 数值 null 同样受治理；builder 遇到稀疏数据
# 必须产出 restricted + limitation 的合法 Draft，而不是在校验边界崩溃。


async def test_selection_builder_item_null_display_field_produces_restricted() -> None:
    from app.agent_artifacts.payloads.kol_selection import KolSelectionV3

    from tests.agent_artifacts.test_kol_selection_builder import LIGHT_CTX, SCOPE, _kol_item

    item = _kol_item(uid="1", engagement_total=100, quoted_price=None)
    build = await build_kol_selection_draft(
        scope=SCOPE, evidence_id="ev-1", items=[item], context=LIGHT_CTX
    )
    payload = build.payload
    assert payload["data"]["items"][0]["quoted_price"] is None  # 不伪造 0
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["items"]["status"] == "partial"
    assert any(
        "items.0.quoted_price" in limitation["affected_paths"]
        for limitation in payload["limitations"]
    )
    KolSelectionV3.model_validate(payload)


def test_analysis_builder_kol_trend_null_produces_restricted() -> None:
    from tests.agent_artifacts.test_kol_analysis_builder import (
        _selection_item,
        _selection_payload,
        _selection_refs,
    )

    items = [_selection_item("1")]
    selection_payload = _selection_payload(items)
    # 名单项缺 avg_engagement（名单 Version 已发布，builder 只读 data.items）。
    selection_payload["data"]["items"][0]["avg_engagement"] = None
    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=selection_payload,
        parent_artifact_version_id="V1",
        selection_version="1",
        selection_refs=_selection_refs(items),
    )
    payload = build.payload
    assert payload["data"]["kol_trend"][0]["avg_engagement"] is None
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["kol_trend"]["status"] == "partial"
    assert payload["availability"]["summary"]["status"] == "complete"
    assert any(
        "kol_trend.0.avg_engagement" in limitation["affected_paths"]
        for limitation in payload["limitations"]
    )
    KolAnalysisV2.model_validate(payload)


def _sparse_detail(**overrides: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "identity": {
            "nickname": "达人K",
            "avatar_url": "https://example.com/a.png",
            "homepage_url": "https://example.com/k1",
            "bio": "美食博主",
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
            "age_distribution": [{"key": "25-34", "label": "25-34", "value": 4, "share": 0.4}],
            "region_distribution": [{"key": "sh", "label": "上海", "value": 3, "share": 0.3}],
            "interest_distribution": [
                {"key": "coffee", "label": "咖啡", "value": 2, "share": 0.2}
            ],
        },
        "trend": [{"date": "2026-01-01", "followers": 100000, "engagement": 500, "posts": 5}],
        "latest_posts": [
            {
                "post_id": "p1",
                "title": "热帖",
                "url": "https://example.com/p1",
                "published_at": "2026-01-01T12:00:00",
                "likes": 100,
                "comments": 10,
                "shares": 5,
                "engagement": 115,
            }
        ],
    }
    detail.update(overrides)
    return detail


_CACHE_STATE = {
    "hit": False,
    "fetched_at": "2026-01-01T12:00:00",
    "expires_at": "2026-01-02T12:00:00",
}


def test_detail_builder_trend_null_produces_restricted() -> None:
    detail = _sparse_detail(
        trend=[{"date": "2026-01-01", "followers": None, "engagement": 500, "posts": 5}]
    )
    build = build_kol_detail_draft(
        platform="xiaohongshu",
        kol_uid="k1",
        detail=detail,
        evidence_id="ev-1",
        cache_state=_CACHE_STATE,
    )
    payload = build.payload
    assert payload["data"]["trend"][0]["followers"] is None
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["trend"]["status"] == "partial"
    assert any(
        limitation["code"] == "trend_partial"
        and "trend.0.followers" in limitation["affected_paths"]
        for limitation in payload["limitations"]
    )
    KolDetailV2.model_validate(payload)


def test_detail_builder_audience_item_null_produces_restricted() -> None:
    detail = _sparse_detail()
    detail["audience"] = dict(detail["audience"])
    detail["audience"]["gender_distribution"] = [
        {"key": "f", "label": "女", "value": None, "share": 0.6}
    ]
    build = build_kol_detail_draft(
        platform="xiaohongshu",
        kol_uid="k1",
        detail=detail,
        evidence_id="ev-1",
        cache_state=_CACHE_STATE,
    )
    payload = build.payload
    assert payload["data"]["audience"]["gender_distribution"][0]["value"] is None
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["audience"]["status"] == "partial"
    assert any(
        "audience.gender_distribution.0.value" in limitation["affected_paths"]
        for limitation in payload["limitations"]
    )
    KolDetailV2.model_validate(payload)


def test_detail_builder_post_metric_null_produces_restricted() -> None:
    detail = _sparse_detail()
    detail["latest_posts"] = [dict(detail["latest_posts"][0], likes=None)]
    build = build_kol_detail_draft(
        platform="xiaohongshu",
        kol_uid="k1",
        detail=detail,
        evidence_id="ev-1",
        cache_state=_CACHE_STATE,
    )
    payload = build.payload
    assert payload["data"]["latest_posts"][0]["likes"] is None
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["latest_posts"]["status"] == "partial"
    assert any(
        limitation["code"] == "post_metric_missing"
        and "latest_posts.0.likes" in limitation["affected_paths"]
        for limitation in payload["limitations"]
    )
    KolDetailV2.model_validate(payload)
