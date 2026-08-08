"""Task 3：营销 Evidence 的 canonical 字段与品牌/活动章节映射。"""

from __future__ import annotations

from typing import Any

from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import build_campaign_report_draft

_BRAND_SCOPE = {
    "brand": "测试品牌",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["测试"],
    "comparison_mode": "none",
}

_CAMPAIGN_SCOPE = {
    "brand": "测试品牌",
    "campaign": "测试活动",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["测试"],
}


def _by_path(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {field["path"]: field for field in payload["canonical_data"]}


def test_brand_sections_are_built_from_canonical_evidence_with_field_lineage() -> None:
    build = build_brand_report_draft(
        scope=_BRAND_SCOPE,
        evidence={
            "overview_current": [
                ("ev-overview", [{"平台": "小红书", "声量": 100, "互动数": 240, "发帖数": 8}])
            ],
            "sentiment": [("ev-sentiment", [{"平台": "小红书", "情感": "正面", "声量": 100}])],
            "daily_trend": [
                ("ev-trend", [{"日期": "2026-07-01", "平台": "小红书", "声量": 10, "互动数": 20}])
            ],
            "top_posts": [
                (
                    "ev-post",
                    [{"平台": "小红书", "帖子ID": "p-1", "标题": "测试帖", "发布时间": "2026-07-01", "互动数": 20}],
                )
            ],
        },
    )

    fields = _by_path(build.payload)
    assert fields["/overview_current/ev-overview/0/声量"] == {
        "path": "/overview_current/ev-overview/0/声量",
        "value": 100,
        "availability": "available",
        "evidence_ids": ["ev-overview"],
        "unit": "mentions",
    }
    assert fields["/daily_trend/ev-trend/0/日期"]["unit"] == "timestamp"
    assert fields["/top_posts/ev-post/0/帖子ID"]["value"] == "p-1"
    assert build.payload["data"]["overview"]["total_volume"] == 100
    assert build.payload["data"]["daily_trend"][0]["volume"] == 10
    assert build.payload["data"]["top_posts"][0]["post_id"] == "p-1"
    assert "/overview_current/ev-overview/0/声量" in build.payload["field_lineage"]["/data/overview/total_volume"]
    assert "/daily_trend/ev-trend/0/声量" in build.payload["field_lineage"]["/data/daily_trend/0/volume"]
    assert any(field["availability"] == "unavailable" for field in fields.values())


def test_campaign_missing_engagement_stays_unavailable_instead_of_zero() -> None:
    build = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={
            "posts": [
                (
                    "ev-post",
                    [
                        {
                            "平台": "小红书",
                            "帖子ID": "p-2",
                            "标题": "无互动字段的测试帖",
                            "发布时间": "2026-07-01",
                        }
                    ],
                )
            ]
        },
    )

    fields = _by_path(build.payload)
    assert build.payload["data"]["overview"]["total_volume"] == 1
    assert build.payload["data"]["overview"]["total_engagement"] is None
    assert build.payload["availability"]["overview"]["status"] == "partial"
    assert fields["/posts/engagement"] == {
        "path": "/posts/engagement",
        "value": None,
        "availability": "unavailable",
        "evidence_ids": [],
        "unit": "interactions",
    }
    assert build.payload["data"]["top_posts"][0]["post_id"] == "p-2"
