"""Task 3R：营销 canonical 契约（对外发布的 CanonicalField + 精确 field_lineage）。

语义要点（设计 §Task 3R）：
- canonical path 必须是稳定的 Artifact 业务路径（``/data/...``），DataTap 原始
  key/source_path 只保留在 Evidence/evidence_refs 层；
- canonical field 的 value 必须等于该 Artifact 路径的最终业务值（同一计算结果）；
- 派生指标（total_volume 等）也必须生成 canonical field，evidence_ids 为参与
  计算的全部 Evidence ID；
- field_lineage 只映射到该最终 canonical field（恒等映射），绝不过宽；
- availability 强类型：complete/partial/unavailable；真实 0 保持 complete+0，
  缺失保持 unavailable+None，部分覆盖为 partial；
- 同一 Evidence 跨多个 group 不重复 canonical field、不重复 Evidence ID。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import build_campaign_report_draft
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


def _by_path(payload: dict[str, Any]) -> dict[str, CanonicalField]:
    return {field.path: field for field in payload["canonical_data"]}


def _brand_evidence(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, Any]]]:
    return {
        "overview_current": [("ev-xhs", [rows[0]])] + [("ev-dy", [row]) for row in rows[1:]],
        "sentiment": [("ev-s", [{"平台": "小红书", "情感": "正面", "声量": 100}])],
        "daily_trend": [
            ("ev-t", [{"日期": "2026-07-01", "平台": "小红书", "声量": 10, "互动数": 20}])
        ],
        "top_posts": [
            (
                "ev-p",
                [{"平台": "小红书", "帖子ID": "p-1", "标题": "测试帖", "发布时间": "2026-07-01", "互动数": 20}],
            )
        ],
    }


def _campaign_post(platform: str, post_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "平台": platform,
        "帖子ID": post_id,
        "标题": f"标题-{post_id}",
        "发布时间": "2026-07-01 09:00:00",
        "互动数": 10,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. 中英文别名生成完全相同的 canonical path
# ---------------------------------------------------------------------------


def test_zh_en_aliases_produce_identical_canonical_paths() -> None:
    zh_rows = [{"平台": "小红书", "声量": 100, "互动数": 240, "发帖数": 8}]
    en_rows = [{"platform": "xiaohongshu", "volume": 100, "engagement": 240, "posts": 8}]
    zh = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=_brand_evidence(zh_rows))
    en = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=_brand_evidence(en_rows))

    zh_paths = set(_by_path(zh.payload))
    en_paths = set(_by_path(en.payload))
    assert zh_paths == en_paths
    # 稳定业务路径，绝不包含供应商原始 key（声量/brand_mentions 等）。
    assert "/data/overview/total_volume" in zh_paths
    assert "/data/daily_trend/0/date" in zh_paths
    assert "/data/daily_trend/0/volume" in zh_paths
    assert not any("声量" in path or "brand_mentions" in path for path in zh_paths)
    assert zh.payload["data"]["overview"]["total_volume"] == en.payload["data"]["overview"]["total_volume"] == 100


# ---------------------------------------------------------------------------
# 2/3/4. 跨平台派生 total_volume：求和、双 Evidence、精确 lineage
# ---------------------------------------------------------------------------


def test_brand_total_volume_sums_across_platforms_and_carries_both_evidences() -> None:
    build = build_brand_report_draft(
        scope=_BRAND_SCOPE,
        evidence=_brand_evidence(
            [
                {"平台": "小红书", "声量": 100, "互动数": 240, "发帖数": 8},
                {"平台": "抖音", "声量": 200, "互动数": 360, "发帖数": 15},
            ]
        ),
    )
    payload = build.payload
    assert payload["data"]["overview"]["total_volume"] == 300

    field = _by_path(payload)["/data/overview/total_volume"]
    assert field.value == 300  # 与最终业务值来自同一计算结果
    assert field.availability == "complete"
    assert field.unit == "mentions"
    assert field.evidence_ids == ("ev-xhs", "ev-dy")  # 两个平台的 Evidence 都在
    # field_lineage 只映射到该最终 canonical field，绝不包含无关字段。
    assert payload["field_lineage"]["/data/overview/total_volume"] == ("/data/overview/total_volume",)
    assert not any(
        path in payload["field_lineage"]["/data/overview/total_volume"]
        for path in ("/data/overview/total_engagement", "/data/overview/total_posts", "platform")
    )
    assert payload["field_lineage"]["/data/overview/total_engagement"] == (
        "/data/overview/total_engagement",
    )


# ---------------------------------------------------------------------------
# 5/6/7. Campaign 派生值、显式 0、缺失字段
# ---------------------------------------------------------------------------


def test_campaign_single_post_derives_total_volume_one() -> None:
    build = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-post", [_campaign_post("小红书", "p-1")])]},
    )
    payload = build.payload
    assert payload["data"]["overview"]["total_volume"] == 1
    field = _by_path(payload)["/data/overview/total_volume"]
    assert field.value == 1
    assert field.availability == "complete"
    assert field.evidence_ids == ("ev-post",)
    assert payload["field_lineage"]["/data/overview/total_volume"] == ("/data/overview/total_volume",)


def test_explicit_zero_stays_complete_zero() -> None:
    build = build_campaign_report_draft(
        scope={**_CAMPAIGN_SCOPE, "platforms": ["xiaohongshu"]},
        evidence={"posts": [("ev-z", [_campaign_post("小红书", "p-0", 互动数=0)])]},
    )
    payload = build.payload
    assert payload["data"]["overview"]["total_engagement"] == 0
    field = _by_path(payload)["/data/overview/total_engagement"]
    assert field.value == 0  # 真实 0 不得转换为 None/unavailable
    assert field.availability == "complete"
    assert field.evidence_ids == ("ev-z",)


def test_missing_field_generates_unavailable_none() -> None:
    build = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={
            "posts": [("ev-post", [_campaign_post("小红书", "p-1", 互动数=None)])]
        },
    )
    payload = build.payload
    assert payload["data"]["overview"]["total_engagement"] is None
    field = _by_path(payload)["/data/overview/total_engagement"]
    assert field.value is None
    assert field.availability == "unavailable"
    assert field.evidence_ids == ()
    assert payload["availability"]["overview"]["status"] == "partial"


# ---------------------------------------------------------------------------
# 8. 部分平台覆盖 → 聚合字段 partial
# ---------------------------------------------------------------------------


def test_partial_platform_coverage_marks_aggregate_partial() -> None:
    build = build_brand_report_draft(
        scope=_BRAND_SCOPE,  # scope 声明两个平台
        evidence=_brand_evidence(
            [{"平台": "小红书", "声量": 100, "互动数": 240, "发帖数": 8}]  # 只覆盖一个平台
        ),
    )
    payload = build.payload
    field = _by_path(payload)["/data/overview/total_volume"]
    assert field.value == 100  # 使用实际可计算值
    assert field.availability == "partial"
    assert field.evidence_ids == ("ev-xhs",)
    assert payload["availability"]["overview"]["status"] == "partial"
    assert any(
        item["code"] == "platform_coverage_incomplete" for item in payload["limitations"]
    )
    assert payload["data_status"] == "restricted"


# ---------------------------------------------------------------------------
# 9. 同一 Evidence 跨多个 group 去重
# ---------------------------------------------------------------------------


def test_same_evidence_across_groups_not_duplicated() -> None:
    rows = [_campaign_post("小红书", "p-1")]
    build = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={
            "posts": [("ev-post", rows)],
            "social": [("ev-post", rows)],  # 同一 Evidence 进入两个 group
        },
    )
    payload = build.payload
    assert payload["data"]["overview"]["total_volume"] == 1  # 未双计
    paths = [field.path for field in payload["canonical_data"]]
    assert len(set(paths)) == len(paths)  # 无重复 canonical path
    field = _by_path(payload)["/data/overview/total_volume"]
    assert field.evidence_ids == ("ev-post",)  # Evidence ID 不重复


# ---------------------------------------------------------------------------
# 10. Campaign 五章节均有 canonical/lineage 覆盖
# ---------------------------------------------------------------------------


def test_campaign_sections_have_canonical_coverage() -> None:
    build = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={
            "posts": [
                (
                    "ev-posts",
                    [
                        _campaign_post("小红书", "p-1", 互动数=115, 情感="正面"),
                        _campaign_post("小红书", "p-2", 互动数=55, 情感="中性"),
                        _campaign_post("抖音", "p-3", 互动数=250, 情感="负面"),
                    ],
                )
            ]
        },
    )
    payload = build.payload
    fields = _by_path(payload)
    for path in (
        "/data/overview/total_volume",
        "/data/platform_contributions/0/volume",
        "/data/timeline/0/volume",
        "/data/sentiment/summary/positive/count",
        "/data/top_posts/0/engagement",
    ):
        assert path in fields, path
        assert path in payload["field_lineage"], path
        assert payload["field_lineage"][path] == (path,)
        assert fields[path].availability == "complete"
        assert fields[path].evidence_ids == ("ev-posts",)
        # canonical value 与最终 payload 值完全一致。
        node: Any = payload["data"]
        for token in path.split("/")[2:]:
            node = node[int(token)] if token.isdigit() else node[token]
        assert fields[path].value == node


# ---------------------------------------------------------------------------
# 11. Top post 缺失 title/url → unavailable 字段（不消失、不伪造空串）
# ---------------------------------------------------------------------------


def test_top_post_missing_title_url_generates_unavailable_fields() -> None:
    build = build_campaign_report_draft(
        scope=_CAMPAIGN_SCOPE,
        evidence={
            "posts": [
                ("ev-p", [_campaign_post("小红书", "p-1", 标题=None, 帖子链接=None)])
            ]
        },
    )
    payload = build.payload
    assert payload["data"]["top_posts"][0]["title"] is None
    assert payload["data"]["top_posts"][0]["url"] is None
    fields = _by_path(payload)
    for path in ("/data/top_posts/0/title", "/data/top_posts/0/url"):
        assert fields[path].value is None
        assert fields[path].availability == "unavailable"
        assert fields[path].evidence_ids == ()
    # 全字段集覆盖：platform/post_id/title/url/author/published_at/likes/comments/shares/engagement。
    expected = {
        "/data/top_posts/0/platform",
        "/data/top_posts/0/post_id",
        "/data/top_posts/0/title",
        "/data/top_posts/0/url",
        "/data/top_posts/0/author",
        "/data/top_posts/0/published_at",
        "/data/top_posts/0/likes",
        "/data/top_posts/0/comments",
        "/data/top_posts/0/shares",
        "/data/top_posts/0/engagement",
    }
    assert expected <= set(fields)


# ---------------------------------------------------------------------------
# 12. wrapped JSON string 与 rows/items/data 容器
# ---------------------------------------------------------------------------


def test_wrapped_json_string_and_row_containers() -> None:
    inner = [{"平台": "小红书", "声量": 10, "互动数": 20, "发帖数": 2}]
    plain = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=_brand_evidence(inner))
    for container in ("rows", "items", "data"):
        wrapped_evidence = _brand_evidence(inner)
        # DataTap 常见形态：整个 Evidence payload 是 {"result": "<json 字符串>"}。
        wrapped_evidence["overview_current"] = [
            ("ev-w", {"result": json.dumps({container: inner}, ensure_ascii=False)})
        ]
        wrapped = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=wrapped_evidence)
        assert set(_by_path(wrapped.payload)) == set(_by_path(plain.payload))
        assert (
            _by_path(wrapped.payload)["/data/overview/total_volume"].value
            == _by_path(plain.payload)["/data/overview/total_volume"].value
            == 10
        )


# ---------------------------------------------------------------------------
# 13. canonical_data 与 evidence_ids 输出顺序稳定
# ---------------------------------------------------------------------------


def test_canonical_output_order_is_stable() -> None:
    evidence = _brand_evidence(
        [
            {"平台": "小红书", "声量": 100, "互动数": 240, "发帖数": 8},
            {"平台": "抖音", "声量": 200, "互动数": 360, "发帖数": 15},
        ]
    )
    first = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence)
    second = build_brand_report_draft(scope=_BRAND_SCOPE, evidence=evidence)
    dumped_first = [field.model_dump() for field in first.payload["canonical_data"]]
    dumped_second = [field.model_dump() for field in second.payload["canonical_data"]]
    assert dumped_first == dumped_second
    total_volume = _by_path(first.payload)["/data/overview/total_volume"]
    assert total_volume.evidence_ids == ("ev-xhs", "ev-dy")  # 保序去重


# ---------------------------------------------------------------------------
# 14. 非法输入 fail-closed
# ---------------------------------------------------------------------------


class _CanonicalOnly(CanonicalPayloadMixin):
    pass


def test_invalid_canonical_inputs_are_rejected() -> None:
    # 非法 availability。
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=1, availability="available", evidence_ids=("e",))
    # path 非空且必须以 /data/ 开头。
    with pytest.raises(ValidationError):
        CanonicalField(path="", value=1, availability="complete", evidence_ids=("e",))
    with pytest.raises(ValidationError):
        CanonicalField(path="/overview/total", value=1, availability="complete", evidence_ids=("e",))
    # unavailable 但 value 非 None。
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=1, availability="unavailable", evidence_ids=("e",))
    # value None 但 availability 不是 unavailable。
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=None, availability="complete")
    # 数值 complete/partial 但 evidence_ids 为空。
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=5, availability="complete", evidence_ids=())
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=5, availability="partial", evidence_ids=())
    # evidence_ids 重复。
    with pytest.raises(ValidationError):
        CanonicalField(path="/data/x", value=5, availability="complete", evidence_ids=("e", "e"))


def test_duplicate_canonical_path_rejected() -> None:
    field = CanonicalField(path="/data/x", value=1, availability="complete", evidence_ids=("e",))
    with pytest.raises(ValidationError):
        _CanonicalOnly(
            canonical_data=(field, CanonicalField(path="/data/x", value=2, availability="complete", evidence_ids=("e",))),
            field_lineage={"/data/x": ("/data/x",)},
        )
