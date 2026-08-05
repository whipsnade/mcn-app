"""四个 Profile 正式 prompt 的内容契约测试（v3 加固 §6.2，B3）。

骨架期只冻结「每个 Profile 有一个带版本的非空 prompt」；本文件冻结正式
prompt 的关键指引片段：

- session_analyst_v1：四动作协议、澄清时机、工具使用准则（MCP 采集
  Evidence → 历史读取/计算 → Builder 构建 → submit_review）、五类 Artifact
  选择指引、Evidence ID 与游标读取、失败处理（unknown 不重放、余额不足
  受限交付）、Builder 优先于手写 payload、restricted 诚实披露；
- artifact_reviewer_v1：审核清单、approve/revise/reject 语义、restricted
  放行条件；
- kol_detail_v1：缓存优先、MCP allowlist、最多 5 条热帖、URL 不伪造、
  无 ask_user；
- utility_v1：标题/摘要/建议输出契约。

设计红线：prompt 不规定固定业务阶段或固定工具顺序——这里用「显式自主
决策声明存在 + 无阶段编号措辞」双向断言守住。
"""

from __future__ import annotations

import re

from app.agent_runtime.profiles import PROFILES
from app.agent_runtime.prompts import get_system_prompt

# 各 Profile 的当前 prompt 版本（内容修订独立递增；本次为叙事防编造强化）。
PROMPT_VERSIONS = {
    "session_analyst_v1": "v3",
    "artifact_reviewer_v1": "v3",
    "kol_detail_v1": "v2",
    "utility_v1": "v2",
}


def _text(name: str) -> str:
    return get_system_prompt(name).text


def test_all_prompts_versioned_with_content() -> None:
    for key in PROFILES:
        prompt = get_system_prompt(key)
        assert prompt.name == key
        assert prompt.version == PROMPT_VERSIONS[key]
        assert len(prompt.text.strip()) > 200  # 正式 prompt，非骨架


# ---------------------------------------------------------------------------
# session_analyst_v1
# ---------------------------------------------------------------------------


def test_session_analyst_prompt_four_action_protocol() -> None:
    text = _text("session_analyst_v1")
    for action in ("ask_user", "call_tool", "submit_review", "complete"):
        assert action in text
    # 澄清时机：信息不足主动问，一次一问，2-4 个选项。
    assert "2-4" in text
    assert "澄清" in text


def test_session_analyst_prompt_tool_usage_guidance() -> None:
    text = _text("session_analyst_v1")
    # MCP 采集 Evidence：evidence_id 句柄 + read_tool_result 游标读取。
    assert "evidence_id" in text
    assert "read_tool_result" in text
    assert "cursor" in text
    # 历史读取与确定性计算。
    assert "read_artifact" in text
    assert "search_evidence" in text
    # 积分成本与钱包余额意识。
    assert "积分" in text
    assert "余额" in text


def test_session_analyst_prompt_artifact_builder_guidance() -> None:
    text = _text("session_analyst_v1")
    # 五类 Artifact 的用途说明与选择指引。
    for schema in (
        "brand_report_v3",
        "campaign_report_v2",
        "kol_selection_v3",
        "kol_analysis_v2",
        "insight_board_v1",
    ):
        assert schema in text
    # 正式六类的 Builder 工具名（含 insight 钻取看板，H5）。
    for tool in (
        "build_brand_report_draft",
        "build_campaign_report_draft",
        "build_kol_selection_draft",
        "build_kol_analysis_draft",
        "build_insight_draft",
    ):
        assert tool in text
    # 正式产物必须用 Builder，不手写整份 payload；create_draft/update_draft
    # 不允许直写任何强类型正式 payload（含 insight_board_v1）。
    assert "create_draft" in text
    assert "update_draft" in text
    assert "不要手写" in text or "不得手写" in text


def test_session_analyst_prompt_failure_handling() -> None:
    text = _text("session_analyst_v1")
    # unknown 不重放；余额不足基于已有证据受限交付；失败换参数/换工具/继续。
    assert "unknown" in text
    assert "不重放" in text or "禁止重放" in text
    assert "受限交付" in text
    # restricted 诚实披露原则。
    assert "restricted" in text
    assert "limitations" in text


def test_session_analyst_prompt_mentions_exemplars() -> None:
    text = _text("session_analyst_v1")
    assert "exemplars" in text


def test_session_analyst_prompt_narrative_anti_fabrication_rule() -> None:
    """叙事防编造：每个数字必须能在 supporting_paths 指向的 data 位置找到同值。"""
    text = _text("session_analyst_v1")
    assert "supporting_paths 指向" in text
    assert "找不到就不要写这个数字" in text


def test_session_analyst_prompt_has_no_fixed_stages_or_tool_order() -> None:
    """设计红线：不规定固定业务阶段或固定工具顺序（显式自主声明 + 无阶段编号）。"""
    text = _text("session_analyst_v1")
    assert "不规定固定业务阶段" in text
    assert "固定工具顺序" in text
    # 无「第一步/第二步…」式固定阶段编排措辞。
    assert not re.search(r"第[一二三四五六七八九十]+步", text)


# ---------------------------------------------------------------------------
# artifact_reviewer_v1
# ---------------------------------------------------------------------------


def test_artifact_reviewer_prompt_checklist_and_decisions() -> None:
    text = _text("artifact_reviewer_v1")
    # 审核清单：完整性 / 数字可追溯 / 引用有效 / 结论不冲突 / 限制披露充分。
    assert "完整" in text
    assert "可追溯" in text
    assert "引用" in text
    assert "冲突" in text
    assert "披露" in text
    # 三种决策语义。
    for decision in ("approve", "revise", "reject"):
        assert decision in text
    # 只读边界：不调用工具、不输出 Agent 动作。
    assert "不调用" in text or "不得调用" in text


def test_artifact_reviewer_prompt_restricted_pass_condition() -> None:
    text = _text("artifact_reviewer_v1")
    assert "restricted" in text
    # 放行条件：缺口如实披露可 approve；隐瞒缺口必须 revise/reject。
    assert "放行" in text


def test_artifact_reviewer_prompt_narrative_number_consistency_check() -> None:
    """narrative 数字与 data 一致性是审核重点：找不到同值按编造处理。"""
    text = _text("artifact_reviewer_v1")
    assert "narrative" in text
    assert "同值" in text
    assert "编造" in text


# ---------------------------------------------------------------------------
# kol_detail_v1
# ---------------------------------------------------------------------------


def test_kol_detail_prompt_contract() -> None:
    text = _text("kol_detail_v1")
    # 缓存优先（服务端先行，缓存未命中才进入本 Run）。
    assert "缓存" in text
    # 允许的 MCP 工具明确名单 + Builder。
    assert "kol_detail" in text
    assert "query_raw_posts" in text
    assert "build_kol_detail_draft" in text
    # 最多 5 条热帖。
    assert "5" in text and "热帖" in text
    # 缺 URL 披露不伪造。
    assert "伪造" in text
    # 无 ask_user。
    assert "ask_user" in text


# ---------------------------------------------------------------------------
# utility_v1
# ---------------------------------------------------------------------------


def test_utility_prompt_output_contract() -> None:
    text = _text("utility_v1")
    for task in ("session_title", "run_summary", "suggestions"):
        assert task in text
    assert "title" in text
    assert "summary" in text
