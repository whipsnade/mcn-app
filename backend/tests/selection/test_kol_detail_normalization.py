from __future__ import annotations

import json
from datetime import date, timedelta

from app.selection.normalizers import normalize_kol_detail_facts


def test_normalizes_batch_detail_scopes_and_groups_recent_trend_by_week() -> None:
    today = date.today()
    first_day = today - timedelta(days=27)
    content = {
        "result": json.dumps(
            {
                "达人详情列表": [
                    {
                        "账号ID (kwUid)": "dy-1",
                        "粉丝数": "10万",
                        "综合评分": 85,
                        "受众画像": {
                            "粉丝年龄分布": {"18-24": "40%", "25-34": "35%"},
                            "粉丝省份分布Top10": {"上海": "18%", "北京": "12%"},
                            "粉丝兴趣分布": {"美食": "45%", "旅行": "30%"},
                            "有效粉丝率": "62%",
                            "活跃粉丝数": "6.2万",
                        },
                        "发帖数据-汇总统计": {
                            "作品数": 28,
                            "平均互动量": "1,200",
                            "平均播放量": "2万",
                        },
                        "账号趋势": [
                            {"日期": first_day.isoformat(), "互动数": 100},
                            {"日期": (first_day + timedelta(days=1)).isoformat(), "互动数": 300},
                            {"日期": (today - timedelta(days=2)).isoformat(), "互动数": 500},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
    }

    details = normalize_kol_detail_facts(
        "datatap.social.grow.kol.detail.v1",
        {
            "platform": "douyin",
            "kwUidList": ["dy-1"],
            "scope": ["fansAudience", "postSummaryStatistics", "accountTrend"],
        },
        content,
    )

    assert len(details) == 1
    detail = details[0]
    assert detail.platform == "douyin"
    assert detail.platform_account_id == "dy-1"
    assert detail.completed_scopes == (
        "fansAudience",
        "postSummaryStatistics",
        "accountTrend",
    )
    assert detail.facts == {
        "followers": 100_000,
        "content_score": 85.0,
        "effective_follower_rate": 62.0,
        "active_follower_count": 62_000,
        "audience_age": {"18-24": 40, "25-34": 35},
        "audience_regions": {"上海": 18, "北京": 12},
        "audience_interests": {"美食": 45, "旅行": 30},
        "works_count": 28,
        "average_interactions": 1200,
        "average_reads": 20_000,
        "average_interaction_per_follower_rate": 1.2,
        "recent_30d_average_interactions": 300.0,
    }
    first_week = first_day - timedelta(days=first_day.weekday())
    last_day = today - timedelta(days=2)
    last_week = last_day - timedelta(days=last_day.weekday())
    assert detail.trend_points == (
        {"week_start": first_week.isoformat(), "average_interactions": 200.0, "post_count": 2},
        {"week_start": last_week.isoformat(), "average_interactions": 500.0, "post_count": 1},
    )


def test_detail_normalizer_omits_invalid_and_future_trend_rows_without_faking_points() -> None:
    today = date.today()
    content = {
        "result": json.dumps(
            {
                "达人详情列表": [
                    {
                        "账号ID (kwUid)": "xhs-1",
                        "账号趋势": [
                            {"日期": "not-a-date", "互动数": 100},
                            {"日期": (today + timedelta(days=1)).isoformat(), "互动数": 200},
                            {"日期": (today - timedelta(days=40)).isoformat(), "互动数": 300},
                            {"日期": today.isoformat(), "互动数": "not-a-number"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
    }

    details = normalize_kol_detail_facts(
        "datatap.social.grow.kol.detail.v1",
        {
            "platform": "xiaohongshu",
            "kwUidList": ["xhs-1"],
            "scope": ["accountTrend"],
        },
        content,
    )

    assert len(details) == 1
    assert details[0].facts == {}
    assert details[0].trend_points == ()
    assert details[0].completed_scopes == ()
