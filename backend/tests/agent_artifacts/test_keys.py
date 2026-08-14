"""Artifact Key 服务端生成规则测试（设计文档 §8.1 ArtifactKey / Task 12）。

覆盖：品牌/活动文本标准化（NFKC、trim、连续空白折叠、英文小写）、scope/question
SHA-256 且与 dict 键顺序无关、各模块 key 格式，以及「模型只能提供业务字段，
不能直接指定数据库 key」的约束。
"""

import hashlib
import json
from uuid import uuid4

import pytest

from app.agent_artifacts.keys import build_artifact_key


def test_brand_key_normalizes_whitespace_and_case() -> None:
    assert build_artifact_key("brand", brand=" 瑞幸  Coffee ") == "brand:瑞幸 coffee"
    # 等价输入（前后空白、多个连续空白）产出同一 key
    assert build_artifact_key("brand", brand="瑞幸 Coffee") == "brand:瑞幸 coffee"
    assert build_artifact_key("brand", brand="\t瑞幸\t  Coffee\n") == "brand:瑞幸 coffee"


def test_brand_key_applies_nfkc_fullwidth_to_ascii() -> None:
    # 全角 Ｃｏｆｆｅｅ → NFKC → Coffee → lowercase → coffee
    assert build_artifact_key("brand", brand="Ｃｏｆｆｅｅ") == "brand:coffee"


def test_campaign_key_combines_normalized_brand_and_campaign() -> None:
    key = build_artifact_key("campaign", brand=" 瑞幸 ", campaign="双十一  Campaign ")
    assert key == "campaign:瑞幸:双十一 campaign"
    assert build_artifact_key("campaign", brand="瑞幸", campaign="双十一 Campaign") == key


def test_kol_selection_scope_hash_is_order_independent() -> None:
    scope_a = {
        "brand": "瑞幸",
        "platforms": ["douyin", "xhs"],
        "audience": {"age_ranges": ["18-24"]},
    }
    scope_b = {
        "audience": {"age_ranges": ["18-24"]},
        "platforms": ["douyin", "xhs"],
        "brand": "瑞幸",
    }
    key_a = build_artifact_key("kol-selection", scope=scope_a)
    key_b = build_artifact_key("kol-selection", scope=scope_b)
    assert key_a == key_b
    assert key_a.startswith("kol-selection:")
    expected = hashlib.sha256(
        json.dumps(scope_a, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert key_a == f"kol-selection:{expected}"


def test_kol_selection_scope_hash_normalizes_string_leaves() -> None:
    # 同一业务 scope 仅空白/大小写不同 → 递归标准化后 hash 一致（§8.1 统一规则）
    scope_a = {
        "brand": " 瑞幸  Coffee ",
        "platforms": ["douyin", "XHS"],
        "audience": {"age_ranges": ["18-24", " 25-34 "]},
    }
    scope_b = {
        "brand": "瑞幸 Coffee",
        "platforms": ["douyin", "xhs"],
        "audience": {"age_ranges": ["18-24", "25-34"]},
    }
    key_a = build_artifact_key("kol-selection", scope=scope_a)
    key_b = build_artifact_key("kol-selection", scope=scope_b)
    assert key_a == key_b
    assert key_a.startswith("kol-selection:")
    # 非字符串叶子（数字/布尔）参与 hash，不因类型误归一化
    assert build_artifact_key(
        "kol-selection", scope={"brand": "瑞幸", "budget_min": 1000}
    ) != build_artifact_key(
        "kol-selection", scope={"brand": "瑞幸", "budget_min": 2000}
    )


def test_kol_selection_scope_hash_changes_with_scope() -> None:
    assert build_artifact_key("kol-selection", scope={"brand": "瑞幸"}) != build_artifact_key(
        "kol-selection", scope={"brand": "星巴克"}
    )


def test_kol_analysis_uses_selection_artifact_id() -> None:
    selection_id = str(uuid4())
    assert build_artifact_key(
        "kol-analysis", selection_artifact_id=selection_id
    ) == f"kol-analysis:{selection_id}"


def test_kol_detail_uses_platform_and_kol_uid() -> None:
    assert build_artifact_key(
        "kol-detail", platform="douyin", kol_uid="1234567890"
    ) == "kol-detail:douyin:1234567890"


def test_insight_uses_parent_version_and_question_hash() -> None:
    parent_version_id = str(uuid4())
    key = build_artifact_key(
        "insight",
        parent_artifact_version_id=parent_version_id,
        question=" 为什么 声量 下降了  ",
    )
    assert key.startswith(f"insight:{parent_version_id}:")
    suffix = key.split(":")[-1]
    assert len(suffix) == 64
    assert int(suffix, 16) >= 0  # 必须是合法 hexdigest
    # 等价问题（trim/折叠空白）产出同一 key
    assert build_artifact_key(
        "insight",
        parent_artifact_version_id=parent_version_id,
        question="为什么 声量 下降了",
    ) == key


def test_insight_hash_depends_on_question_and_parent_version() -> None:
    parent = str(uuid4())
    assert build_artifact_key(
        "insight", parent_artifact_version_id=parent, question="A"
    ) != build_artifact_key("insight", parent_artifact_version_id=parent, question="B")
    assert build_artifact_key(
        "insight", parent_artifact_version_id="v1", question="A"
    ) != build_artifact_key("insight", parent_artifact_version_id="v2", question="A")


def test_key_api_rejects_raw_artifact_key_from_model() -> None:
    # 模型只能提供业务字段，不能直接指定数据库 key
    with pytest.raises(TypeError):
        build_artifact_key("brand", brand="瑞幸", artifact_key="brand:hacked")  # type: ignore[call-arg]


def test_unknown_module_rejected() -> None:
    with pytest.raises(ValueError):
        build_artifact_key("not-a-module", brand="瑞幸")
