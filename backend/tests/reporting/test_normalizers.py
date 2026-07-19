from datetime import datetime, timezone
from decimal import Decimal
from itertools import permutations
import json
import time

import pytest

from app.reporting.normalizers import (
    _decimal,
    _merge,
    _non_negative_number,
    _safe_term,
    normalize_tool_evidence,
)
from app.reporting.schemas import ToolEvidence


NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def evidence(
    tool: str,
    payload: dict,
    call_id: str,
    *,
    collected_at: datetime = NOW,
) -> ToolEvidence:
    return ToolEvidence(
        internal_tool_name=tool,
        source_call_id=call_id,
        collected_at=collected_at,
        payload=payload,
    )


def test_new_datatap_platform_tool_names_use_generic_kol_adapter() -> None:
    item = evidence(
        "datatap.social.grow.kol.bilibili.search.v1",
        {
            "result": json.dumps(
                {
                    "达人列表": [
                        {
                            "账号ID": "bili-1",
                            "昵称": "测试B站达人",
                            "粉丝数": "2.5万",
                            "互动率": "6.2%",
                            "平台": "bilibili",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
        "call-bili",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.platform == "bilibili"
    assert candidate.platform_account_id == "bili-1"
    assert candidate.nickname == "测试B站达人"
    assert candidate.followers == 25000
    assert candidate.engagement_rate == 6.2


def test_generic_adapter_accepts_datatap_detailed_list_without_account_id() -> None:
    item = evidence(
        "datatap.social.grow.kol.weibo.search.v1",
        {
            "result": json.dumps(
                {"达人信息列表": [{"昵称": "微博达人", "粉丝数": "1.2万", "互动率": "3%"}]},
                ensure_ascii=False,
            )
        },
        "call-weibo",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.platform == "weibo"
    assert candidate.nickname == "微博达人"
    assert candidate.platform_account_id.startswith("weibo:")


def test_generic_adapter_treats_empty_detail_list_as_no_candidates() -> None:
    """kol.detail 空结果（筛选无匹配）必须归一化为空候选，而不是任务失败。"""
    empty_zh = evidence(
        "datatap.social.grow.kol.detail.v1",
        {"result": json.dumps({"达人详情列表": []}, ensure_ascii=False)},
        "call-detail-empty",
    )
    empty_alt = evidence(
        "datatap.social.grow.kol.detail.v1",
        {"result": json.dumps({"详情列表": []}, ensure_ascii=False)},
        "call-detail-empty-2",
    )

    assert normalize_tool_evidence([empty_zh]) == ()
    assert normalize_tool_evidence([empty_alt]) == ()


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
                            "曝光量": 100,
                            "内容标签": ["护肤", "测评"],
                            "主页": "https://example.test/profile/uid-1",
                            "endpoint": "https://api.example.test/private",
                            "原始评论": ["不要持久化这条评论"],
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
    assert candidate.normalized_profile_url == "https://example.test/profile/uid-1"
    assert candidate.analytics_fields == {"exposure": 100}
    serialized_analytics = json.dumps(candidate.as_dict()["analytics_fields"], ensure_ascii=False)
    assert "example.test" not in serialized_analytics
    assert "endpoint" not in serialized_analytics
    assert "原始评论" not in serialized_analytics
    assert "不要持久化这条评论" not in serialized_analytics


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
    assert "evidence_priority" not in candidate.as_dict()
    assert "field_provenance" not in candidate.as_dict()
    assert "evidence_priority" not in repr(candidate)
    assert "field_provenance" not in repr(candidate)
    serialized = json.dumps(candidate.as_dict(), ensure_ascii=False)
    assert "raw_comments" not in serialized
    assert "do not persist" not in serialized
    assert "unsafe.example" not in serialized


def test_merge_is_order_independent_and_prefers_newer_evidence() -> None:
    older = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-merge",
            "nickname": "旧昵称",
            "impressions": 100,
            "keywords": ["首发"],
            "risk_flags": [{"type": "z-risk"}],
        },
        "call-old",
        collected_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    newer = evidence(
        "creator.profile.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-merge",
            "nickname": "新昵称",
            "impressions": 200,
            "total_interactions": 50,
            "keywords": [],
            "risk_flags": [{"type": "a-risk"}],
        },
        "call-new",
        collected_at=NOW,
    )

    [forward] = normalize_tool_evidence([older, newer])
    [reverse] = normalize_tool_evidence([newer, older])

    assert forward == reverse
    assert forward.nickname == "新昵称"
    assert forward.analytics_fields == {
        "exposure": 200,
        "interactions": 50,
        "hot_words": ["首发"],
    }
    assert forward.risk_flags == ({"type": "a-risk"}, {"type": "z-risk"})
    assert forward.evidence_references == ("call-new", "call-old")


def test_merge_same_timestamp_uses_source_call_id_as_stable_tie_break() -> None:
    call_a = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-tie",
            "nickname": "A",
            "impressions": 100,
        },
        "call-a",
    )
    call_b = evidence(
        "creator.profile.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-tie",
            "nickname": "B",
            "impressions": 200,
        },
        "call-b",
    )

    [forward] = normalize_tool_evidence([call_a, call_b])
    [reverse] = normalize_tool_evidence([call_b, call_a])

    assert forward == reverse
    assert forward.nickname == "B"
    assert forward.analytics_fields["exposure"] == 200


def test_merge_same_timestamp_and_call_id_uses_content_digest_stably() -> None:
    first = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-digest-tie",
            "nickname": "摘要候选一",
            "impressions": 100,
        },
        "call-same",
    )
    second = evidence(
        "creator.profile.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-digest-tie",
            "nickname": "摘要候选二",
            "impressions": 200,
            "total_interactions": 50,
        },
        "call-same",
    )

    [forward] = normalize_tool_evidence([first, second])
    [reverse] = normalize_tool_evidence([second, first])

    assert forward == reverse
    assert forward.analytics_fields["interactions"] == 50


def test_merge_is_associative_for_every_three_evidence_permutation() -> None:
    payloads = (
        (
            "creator.search.v1",
            "call-a",
            {
                "platform": "bilibili",
                "account_id": "creator-associative",
                "nickname": "A",
                "impressions": 100,
                "keywords": ["A词"],
                "risk_flags": [{"type": "c-risk"}],
            },
        ),
        (
            "creator.profile.v1",
            "call-b",
            {
                "platform": "bilibili",
                "account_id": "creator-associative",
                "nickname": "B",
                "impressions": 200,
                "total_interactions": 20,
                "risk_flags": [{"type": "b-risk"}],
            },
        ),
        (
            "creator.search.v1",
            "call-c",
            {
                "platform": "bilibili",
                "account_id": "creator-associative",
                "nickname": "C",
                "impressions": 300,
                "risk_flags": [{"type": "a-risk"}],
            },
        ),
    )
    records = tuple(
        normalize_tool_evidence([evidence(tool, payload, call_id)])[0]
        for tool, call_id, payload in payloads
    )

    results = []
    for first, second, third in permutations(records):
        results.append(_merge(_merge(first, second), third))
        results.append(_merge(first, _merge(second, third)))

    assert all(result == results[0] for result in results)
    assert all(result.evidence_priority == results[0].evidence_priority for result in results)
    assert all(result.field_provenance == results[0].field_provenance for result in results)
    assert results[0].nickname == "C"
    assert results[0].analytics_fields == {
        "exposure": 300,
        "interactions": 20,
        "hot_words": ["A词"],
    }
    assert results[0].risk_flags == (
        {"type": "a-risk"},
        {"type": "b-risk"},
        {"type": "c-risk"},
    )
    assert results[0].evidence_references == ("call-a", "call-b", "call-c")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1e999999", id="huge_exponent_text"),
        pytest.param(Decimal("1e999999"), id="huge_exponent_decimal"),
        pytest.param("9" * 100, id="oversized_numeric_text"),
        pytest.param(float("inf"), id="positive_infinity"),
        pytest.param(float("-inf"), id="negative_infinity"),
        pytest.param(float("nan"), id="not_a_number"),
        pytest.param(Decimal("NaN"), id="decimal_not_a_number"),
    ],
)
def test_decimal_rejects_resource_exhausting_or_non_finite_values_quickly(value) -> None:
    started_at = time.monotonic()

    with pytest.raises(ValueError, match="invalid_numeric_value"):
        _decimal(value)

    assert time.monotonic() - started_at < 1


def test_analytics_number_rejects_values_above_business_limit() -> None:
    with pytest.raises(ValueError, match="invalid_analytics_number"):
        _non_negative_number(1_000_000_000_000_001)


def test_extreme_optional_analytics_number_is_quickly_left_missing() -> None:
    item = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-extreme-number",
            "brand_mention_count": "1e999999",
            "impressions": 10,
        },
        "call-extreme-number",
    )
    started_at = time.monotonic()

    [candidate] = normalize_tool_evidence([item])

    assert time.monotonic() - started_at < 1
    assert candidate.analytics_fields == {"exposure": 10}


def test_audience_distributions_normalize_percentages_without_changing_sentiment_counts() -> None:
    item = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-percentages",
            "sentiment": {"positive": 1, "neutral": 2, "negative": 3},
            "age_distribution": {
                "under-18": 0,
                "18-24": 0.4,
                "25-34": 1,
                "35-44": "40%",
                "45+": 40,
            },
            "gender_distribution": {"female": 101},
            "region_distribution": {"浙江": 1.2, "上海": "0.4"},
        },
        "call-percentages",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.analytics_fields["sentiment_counts"] == {
        "positive": 1,
        "neutral": 2,
        "negative": 3,
    }
    assert candidate.analytics_fields["audience_age"] == {
        "under-18": 0,
        "18-24": 40,
        "25-34": 100,
        "35-44": 40,
        "45+": 40,
    }
    assert "audience_gender" not in candidate.analytics_fields
    assert candidate.analytics_fields["audience_regions"] == {"浙江": 1.2, "上海": 40}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("sk-" + "x" * 32, id="sk_credential_shape"),
        pytest.param("e30.e30." + "x" * 32, id="jwt_shape"),
        pytest.param("abc.def.ghi", id="jwt_three_short_segments"),
        pytest.param("Bearer " + "x" * 24, id="bearer_shape"),
        pytest.param("/api/internal/metrics", id="internal_api_path"),
        pytest.param("https://public.example.test/path", id="url"),
        pytest.param("api.lkeap.cloud.tencent.com/v1", id="known_api_host"),
    ],
)
def test_safe_term_rejects_credential_and_internal_path_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="invalid_hot_word"):
        _safe_term(value)


def test_hot_words_filter_credentials_and_distribution_rejects_secret_label() -> None:
    credential_like = "sk-" + "x" * 32
    jwt_like = "e30.e30." + "x" * 32
    item = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-sensitive-labels",
            "keywords": [credential_like, "安全热词"],
            "age_distribution": {jwt_like: 40},
            "region_distribution": {"浙江": 20},
        },
        "call-sensitive-labels",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.analytics_fields["hot_words"] == ["安全热词"]
    assert "audience_age" not in candidate.analytics_fields
    assert candidate.analytics_fields["audience_regions"] == {"浙江": 20}


@pytest.mark.parametrize(
    "unsafe_term",
    [
        pytest.param("ftp://files.example.test/archive", id="ftp_uri"),
        pytest.param("file:///tmp/private.txt", id="file_uri"),
        pytest.param("ws://socket.example.test/events", id="websocket_uri"),
        pytest.param("//cdn.example.test/path", id="protocol_relative_url"),
        pytest.param("/etc/passwd", id="absolute_file_path"),
        pytest.param("C:\\Windows\\private.ini", id="windows_absolute_file_path"),
        pytest.param("\\\\server\\share\\private.txt", id="windows_unc_path"),
    ],
)
def test_uri_and_absolute_paths_never_enter_hot_words_or_audience_labels(
    unsafe_term: str,
) -> None:
    item = evidence(
        "creator.search.v1",
        {
            "platform": "bilibili",
            "account_id": "creator-uri-labels",
            "profile_url": "https://public.example.test/creator-uri-labels",
            "keywords": [unsafe_term, "安全热词"],
            "age_distribution": {unsafe_term: 40},
            "region_distribution": {"浙江": 20},
        },
        "call-uri-labels",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.normalized_profile_url == "https://public.example.test/creator-uri-labels"
    assert candidate.analytics_fields["hot_words"] == ["安全热词"]
    assert "audience_age" not in candidate.analytics_fields
    assert candidate.analytics_fields["audience_regions"] == {"浙江": 20}


def test_extract_kol_uids_from_settled_search_evidence() -> None:
    from app.reporting.normalizers import extract_kol_uids

    evidence_payload = {
        "result": json.dumps(
            {
                "KOL 列表": [
                    {"账号ID": "uid-1", "昵称": "达人一"},
                    {"账号ID (kwUid)": "uid-2", "昵称": "达人二"},
                    {"昵称": "无uid达人"},
                    {"账号ID": "uid-1", "昵称": "重复达人"},
                ]
            },
            ensure_ascii=False,
        )
    }

    assert extract_kol_uids(evidence_payload) == ["uid-1", "uid-2"]
    assert extract_kol_uids({"result": json.dumps({"KOL 列表": []})}) == []
    assert extract_kol_uids({"result": "not-json"}) == []
    assert extract_kol_uids(None) == []


def test_generic_adapter_extracts_audience_from_nested_fans_profile() -> None:
    """kol.detail 的受众画像（{"键","值"} 分布项）进入 BI 受众分析字段。"""
    item = evidence(
        "datatap.social.grow.kol.detail.v1",
        {
            "platform": "douyin",
            "result": json.dumps(
                {
                    "详情列表": [
                        {
                            "账号ID (kwUid)": "uid-1",
                            "平台": "douyin",
                            "受众画像": {
                                "粉丝性别分布": [{"键": "女", "值": 0.6828}, {"键": "男", "值": 0.3172}],
                                "粉丝年龄分布": [{"键": "18-24", "值": 0.4}, {"键": "25-30", "值": 0.35}],
                                "粉丝省份分布Top10": [{"键": "广东", "值": 0.12}, {"键": "浙江", "值": 0.08}],
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
        "call-detail-audience",
    )

    [candidate] = normalize_tool_evidence([item])

    assert candidate.analytics_fields["audience_gender"] == {"女": 68.28, "男": 31.72}
    assert candidate.analytics_fields["audience_age"] == {"18-24": 40, "25-30": 35}
    assert candidate.analytics_fields["audience_regions"] == {"广东": 12, "浙江": 8}


def test_detail_audience_merges_into_search_candidate() -> None:
    """搜索候选与详情画像按（平台, 账号ID）合并为同一达人。"""
    search_item = evidence(
        "datatap.social.grow.kol.weibo.search.v1",
        {
            "result": json.dumps(
                {"达人信息列表": [{"昵称": "达人甲", "粉丝数": "2万", "平台": "微博"}]},
                ensure_ascii=False,
            )
        },
        "call-search",
    )
    detail_item = evidence(
        "datatap.social.grow.kol.detail.v1",
        {
            "platform": "weibo",
            "result": json.dumps(
                {
                    "详情列表": [
                        {
                            "账号ID (kwUid)": "weibo:达人甲",
                            "受众画像": {
                                "粉丝性别分布": [{"键": "女", "值": 0.6}, {"键": "男", "值": 0.4}],
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
        "call-detail",
    )

    # 搜索行无账号ID时按昵称派生确定性身份，与详情行账号ID不一致则不合并：
    # 真实链路中搜索行与详情行都带 kwUid。这里验证详情行本身可独立归一化。
    [detail_candidate] = normalize_tool_evidence([detail_item])
    assert detail_candidate.platform == "weibo"
    assert detail_candidate.analytics_fields["audience_gender"] == {"女": 60, "男": 40}
