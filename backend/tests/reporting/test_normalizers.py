from datetime import datetime, timezone
import json

from app.reporting.normalizers import normalize_tool_evidence
from app.reporting.schemas import ToolEvidence


NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def evidence(tool: str, payload: dict, call_id: str) -> ToolEvidence:
    return ToolEvidence(
        internal_tool_name=tool,
        source_call_id=call_id,
        collected_at=NOW,
        payload=payload,
    )


def test_datatap_candidate_exports_template_fields_without_raw_urls() -> None:
    evidence = ToolEvidence(
        internal_tool_name="datatap.xiaohongshu.kol.search.v1",
        source_call_id="call-1",
        collected_at=datetime.now(timezone.utc),
        payload={
            "result": json.dumps(
                {
                    "KOL 列表": [
                        {
                            "账号ID (kwUid)": "uid-1",
                            "昵称": "测试达人",
                            "粉丝数": "2.5万",
                            "城市": "浙江",
                            "总获赞": 12345,
                            "内容标签": ["护肤", "测评"],
                            "主页": "https://example.test/profile/uid-1",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
    )

    [candidate] = normalize_tool_evidence([evidence])

    assert candidate.export_fields == {
        "city": "浙江",
        "total_likes": 12345,
        "content_tags": ["护肤", "测评"],
    }
    assert "主页" not in candidate.export_fields
    assert "export_fields" in candidate.as_dict()


def test_xiaohongshu_candidate_normalizes_safe_analytics_aliases() -> None:
    item = evidence(
        "datatap.xiaohongshu.kol.search.v1",
        {
            "result": json.dumps(
                {
                    "KOL 列表": [
                        {
                            "账号ID (kwUid)": "xhs-1",
                            "品牌提及数": "12",
                            "曝光量": "3.2万",
                            "互动量": 456,
                            "发布时间": "2026-07-15 08:30:00",
                            "舆情分布": {"正向": 10, "中立": "2", "负向": 1, "其他": 999},
                            "热词": [
                                {"词": "种草", "频次": 8},
                                {"词": "https://unsafe.example", "频次": 7},
                            ],
                            "粉丝年龄分布": {"18-24": 45.5, "25-34": "30"},
                            "粉丝性别分布": {"女性": 80, "男性": 20},
                            "粉丝地域分布": {"浙江": 35, "上海": 10},
                            "原始评论": ["这个产品真好", "token=secret"],
                            "api_key": "secret-key",
                            "接口地址": "https://unsafe.example/api",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
        "call-xhs",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.analytics_fields == {
        "brand_mentions": 12,
        "exposure": 32000,
        "interactions": 456,
        "published_at": "2026-07-15T08:30:00",
        "sentiment_counts": {"positive": 10, "neutral": 2, "negative": 1},
        "hot_words": [{"term": "种草", "count": 8}],
        "audience_age": {"18-24": 45.5, "25-34": 30},
        "audience_gender": {"女性": 80, "男性": 20},
        "audience_regions": {"浙江": 35, "上海": 10},
    }
    serialized = json.dumps(candidate.as_dict(), ensure_ascii=False)
    assert "原始评论" not in serialized
    assert "这个产品真好" not in serialized
    assert "secret-key" not in serialized
    assert "unsafe.example" not in serialized


def test_douyin_candidate_normalizes_platform_aliases_and_skips_invalid_optional_fields() -> None:
    item = evidence(
        "datatap.douyin.kol.search.v1",
        {
            "result": json.dumps(
                {
                    "KOL 列表": [
                        {
                            "账号ID (kwUid)": "dy-1",
                            "品牌声量": 6,
                            "播放量": "1.5万",
                            "互动数": -1,
                            "发布日期": ["2026-07-13", "2026-07-14"],
                            "情感统计": {"积极": 9, "中性": 3, "消极": 2},
                            "热点词": ["测评", "Bearer secret-token", "新品"],
                            "年龄分布": {"18-24": 40, "错误": -2},
                            "性别分布": {"女": 70, "男": 30},
                            "地域分布": {"浙江省": 25},
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
        "call-dy",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.platform_account_id == "dy-1"
    assert candidate.analytics_fields == {
        "brand_mentions": 6,
        "exposure": 15000,
        "published_at": ["2026-07-13", "2026-07-14"],
        "sentiment_counts": {"positive": 9, "neutral": 3, "negative": 2},
        "hot_words": ["测评", "新品"],
        "audience_gender": {"女": 70, "男": 30},
        "audience_regions": {"浙江省": 25},
    }
    assert "interactions" not in candidate.analytics_fields
    assert "audience_age" not in candidate.analytics_fields


def test_creator_candidate_normalizes_english_aliases_and_persists_as_dict() -> None:
    item = evidence(
        "creator.search.v1",
        {
            "candidates": [
                {
                    "platform": "bilibili",
                    "account_id": "creator-1",
                    "brand_mention_count": 4,
                    "impressions": 1200,
                    "total_interactions": 88,
                    "publish_dates": ["2026-07-11T10:00:00+08:00"],
                    "sentiment": {"positive": 5, "neutral": 2, "negative": 1, "raw": 99},
                    "keywords": [{"word": "开箱", "frequency": 3}],
                    "age_distribution": {"18-24": 60},
                    "gender_distribution": {"female": 55, "male": 45},
                    "region_distribution": {"Zhejiang": 20},
                    "raw_comments": ["do not persist"],
                    "endpoint": "https://unsafe.example/api",
                }
            ]
        },
        "call-creator",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.analytics_fields["brand_mentions"] == 4
    assert candidate.analytics_fields["hot_words"] == [{"term": "开箱", "count": 3}]
    assert candidate.as_dict()["analytics_fields"] == candidate.analytics_fields
    serialized = json.dumps(candidate.as_dict(), ensure_ascii=False)
    assert "raw_comments" not in serialized
    assert "do not persist" not in serialized
    assert "unsafe.example" not in serialized


def test_merge_fills_only_missing_analytics_fields_deterministically() -> None:
    first = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-merge",
            "impressions": 100,
            "keywords": ["首发"],
        },
        "call-2",
    )
    second = evidence(
        "creator.profile.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-merge",
            "impressions": 200,
            "total_interactions": 50,
            "keywords": [],
        },
        "call-1",
    )

    [candidate] = normalize_tool_evidence([first, second])

    assert candidate.analytics_fields == {
        "exposure": 100,
        "hot_words": ["首发"],
        "interactions": 50,
    }
    assert candidate.evidence_references == ("call-1", "call-2")
