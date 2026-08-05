"""Agent Profile 注册表（设计文档 §五）的契约测试。

Profile 限定能力，不编码业务调用顺序。字段集合本身是结构性保证：
四个 Profile 必须共享完全相同的字段名，不能有任何可承载固定工具序列
或业务阶段的字段。
"""

import dataclasses

import pytest

from app.agent_runtime.profiles import (
    ARTIFACT_TOOLS,
    CALCULATION_TOOLS,
    HISTORY_TOOLS,
    KOL_DETAIL_MCP_TOOL_ALLOWLIST,
    KOL_DETAIL_TOOLS,
    MCP_TOOLS,
    TOOL_CATEGORIES,
    AgentProfile,
    PROFILES,
    get_profile,
)
from app.agent_runtime.prompts import get_system_prompt

FOUR_ACTIONS = frozenset({"ask_user", "call_tool", "publish_artifacts", "complete"})

EXPECTED_FIELDS = {
    "name",
    "version",
    "allowed_actions",
    "allowed_tool_categories",
    "mcp_tool_allowlist",
    "requires_reviewer",
    "max_context_budget",
    "output_schema",
    "system_prompt_key",
}


def test_four_profiles_registered() -> None:
    assert set(PROFILES) == {
        "session_analyst_v1",
        "artifact_reviewer_v1",
        "kol_detail_v1",
        "utility_v1",
    }


@pytest.mark.parametrize(
    ("key", "name", "version"),
    [
        ("session_analyst_v1", "session_analyst", "v1"),
        ("artifact_reviewer_v1", "artifact_reviewer", "v1"),
        ("kol_detail_v1", "kol_detail", "v1"),
        ("utility_v1", "utility", "v1"),
    ],
)
def test_each_profile_has_exact_name_and_version(key: str, name: str, version: str) -> None:
    profile = PROFILES[key]
    assert profile.name == name
    assert profile.version == version
    assert profile.full_name == key


def test_session_analyst_allows_all_four_actions() -> None:
    profile = PROFILES["session_analyst_v1"]
    assert profile.allowed_actions == FOUR_ACTIONS
    # 新执行路径直接发布，无 Reviewer。
    assert profile.requires_reviewer is False


def test_artifact_reviewer_outputs_review_decision_and_no_tools() -> None:
    profile = PROFILES["artifact_reviewer_v1"]
    # Reviewer 已从新执行路径下线；Profile 仅保留供历史代码导入。
    # 审核决策（approve/revise/reject）是独立类型，不属于四种动作协议。
    assert profile.allowed_actions == frozenset()
    assert "call_tool" not in profile.allowed_actions
    assert profile.output_schema == "review_decision"
    assert profile.requires_reviewer is False


def test_kol_detail_actions_and_no_reviewer() -> None:
    profile = PROFILES["kol_detail_v1"]
    assert profile.allowed_actions <= FOUR_ACTIONS
    assert profile.allowed_actions == frozenset({"call_tool", "publish_artifacts", "complete"})
    assert profile.requires_reviewer is False


def test_utility_complete_only_no_reviewer() -> None:
    profile = PROFILES["utility_v1"]
    assert profile.allowed_actions == frozenset({"complete"})
    assert profile.requires_reviewer is False


def test_session_analyst_tool_categories() -> None:
    profile = PROFILES["session_analyst_v1"]
    assert profile.allowed_tool_categories == frozenset(
        {MCP_TOOLS, HISTORY_TOOLS, CALCULATION_TOOLS, ARTIFACT_TOOLS}
    )


def test_artifact_reviewer_has_no_tools() -> None:
    profile = PROFILES["artifact_reviewer_v1"]
    assert profile.allowed_tool_categories == frozenset()


def test_kol_detail_tool_categories() -> None:
    profile = PROFILES["kol_detail_v1"]
    assert profile.allowed_tool_categories == frozenset(
        {MCP_TOOLS, KOL_DETAIL_TOOLS, ARTIFACT_TOOLS}
    )


def test_kol_detail_mcp_allowlist_is_explicit() -> None:
    # kol_detail_v1 的 MCP 访问是明确名单（达人详情 + 原帖/热帖），不是整个分类。
    profile = PROFILES["kol_detail_v1"]
    assert profile.mcp_tool_allowlist == KOL_DETAIL_MCP_TOOL_ALLOWLIST
    assert "kol_detail" in profile.mcp_tool_allowlist
    assert "query_raw_posts" in profile.mcp_tool_allowlist
    # 其他 Profile 不受名单限制。
    assert PROFILES["session_analyst_v1"].mcp_tool_allowlist is None


def test_utility_has_no_tools() -> None:
    profile = PROFILES["utility_v1"]
    assert profile.allowed_tool_categories == frozenset()


def test_tool_categories_are_a_set_not_a_sequence() -> None:
    for profile in PROFILES.values():
        # 分类是集合而非有序序列：仍然满足「Profile 不含固定工具调用顺序」。
        assert isinstance(profile.allowed_tool_categories, frozenset)


def test_unknown_tool_category_rejected() -> None:
    with pytest.raises(ValueError):
        AgentProfile(
            name="bad",
            version="v1",
            allowed_actions=frozenset({"complete"}),
            allowed_tool_categories=frozenset({"typo_category"}),
            requires_reviewer=False,
            max_context_budget=8_000,
            output_schema="utility_json",
            system_prompt_key="bad_v1",
        )


def test_all_registered_categories_are_in_vocabulary() -> None:
    for profile in PROFILES.values():
        assert profile.allowed_tool_categories.issubset(TOOL_CATEGORIES)


def test_profile_fields_are_frozen_schema() -> None:
    # 任何 Profile 都不允许携带业务阶段或固定工具序列字段。
    for profile in PROFILES.values():
        field_names = {field.name for field in dataclasses.fields(profile)}
        assert field_names == EXPECTED_FIELDS
        assert "stages" not in field_names
        assert "tool_sequence" not in field_names


def test_all_profiles_are_frozen_dataclasses() -> None:
    for profile in PROFILES.values():
        assert isinstance(profile, AgentProfile)
        assert dataclasses.is_dataclass(profile)


def test_registry_lookup_by_name() -> None:
    assert get_profile("session_analyst_v1") is PROFILES["session_analyst_v1"]
    assert get_profile("utility_v1") is PROFILES["utility_v1"]


def test_registry_unknown_name_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        get_profile("nope_v9")


def test_all_profiles_have_versioned_prompts() -> None:
    for key, profile in PROFILES.items():
        prompt = get_system_prompt(profile.system_prompt_key)
        assert prompt.name == key
        # prompt 版本随内容修订独立递增（B3 起与 Profile 能力版本解耦：
        # Profile 不变、prompt 内容成熟时只递增 prompt.version）。
        assert prompt.version
        assert prompt.text.strip()
