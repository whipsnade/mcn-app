"""统一模型动作协议（设计文档 §六）的契约测试。

模型每轮决策必须输出且仅输出四种动作之一，由 ``action`` 字段判别；
未知字段一律拒绝（extra="forbid"），保证引擎只能消费冻结的协议。
"""

import pytest
from pydantic import ValidationError

from app.agent_runtime.schemas import (
    AGENT_ACTION_ADAPTER,
    AskUser,
    CallTool,
    Complete,
    PublishArtifacts,
)


def test_ask_user_valid() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python(
        {"action": "ask_user", "question": "Which platform?", "options": ["抖音", "小红书"]}
    )
    assert isinstance(action, AskUser)
    assert action.action == "ask_user"
    assert action.question == "Which platform?"
    assert action.options == ["抖音", "小红书"]


def test_ask_user_options_absent_ok() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python({"action": "ask_user", "question": "Which platform?"})
    assert isinstance(action, AskUser)
    assert action.options is None


@pytest.mark.parametrize("n", [1, 5])
def test_ask_user_options_wrong_length_rejected(n: int) -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {"action": "ask_user", "question": "q", "options": [str(i) for i in range(n)]}
        )


@pytest.mark.parametrize("n", [2, 3, 4])
def test_ask_user_options_length_accepted(n: int) -> None:
    action = AGENT_ACTION_ADAPTER.validate_python(
        {"action": "ask_user", "question": "q", "options": [str(i) for i in range(n)]}
    )
    assert isinstance(action, AskUser)
    assert action.options == [str(i) for i in range(n)]


def test_call_tool_valid() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python(
        {
            "action": "call_tool",
            "internal_tool_name": "kol_detail",
            "arguments": {"platform": "douyin", "uid": "123"},
            "rationale": "Fetch KOL detail",
        }
    )
    assert isinstance(action, CallTool)
    assert action.action == "call_tool"
    assert action.internal_tool_name == "kol_detail"
    assert action.arguments == {"platform": "douyin", "uid": "123"}


def test_call_tool_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {
                "action": "call_tool",
                "internal_tool_name": "kol_detail",
                "arguments": {},
                "rationale": "r",
                "extra": 1,
            }
        )


def test_call_tool_missing_rationale_rejected() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {"action": "call_tool", "internal_tool_name": "t", "arguments": {}}
        )


def test_publish_artifacts_action_accepts_unique_non_empty_ids() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python(
        {
            "action": "publish_artifacts",
            "artifact_draft_ids": ["draft-1", "draft-2"],
            "summary": "品牌报告和达人名单已准备发布",
        }
    )
    assert isinstance(action, PublishArtifacts)
    assert action.action == "publish_artifacts"
    assert tuple(action.artifact_draft_ids) == ("draft-1", "draft-2")


def test_publish_artifacts_empty_draft_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {
                "action": "publish_artifacts",
                "artifact_draft_ids": [],
                "summary": "summary",
            }
        )


def test_submit_review_is_no_longer_an_action() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {
                "action": "submit_review",
                "artifact_draft_ids": ["draft-1"],
                "completion_text": "done",
                "summary": "done",
            }
        )


def test_complete_valid() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python({"action": "complete", "text": "全部完成"})
    assert isinstance(action, Complete)
    assert action.action == "complete"
    assert action.text == "全部完成"
    assert action.suggestions is None


def test_complete_suggestions_optional() -> None:
    action = AGENT_ACTION_ADAPTER.validate_python(
        {"action": "complete", "text": "done", "suggestions": ["查看品牌报告"]}
    )
    assert isinstance(action, Complete)
    assert action.suggestions == ["查看品牌报告"]


def test_complete_text_required() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python({"action": "complete"})


def test_unknown_action_rejected() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python({"action": "dance", "text": "hi"})


def test_each_action_carries_its_literal() -> None:
    payloads = [
        {"action": "ask_user", "question": "q"},
        {
            "action": "call_tool",
            "internal_tool_name": "t",
            "arguments": {},
            "rationale": "r",
        },
        {
            "action": "publish_artifacts",
            "artifact_draft_ids": ["d"],
            "summary": "s",
        },
        {"action": "complete", "text": "t"},
    ]
    for payload in payloads:
        parsed = AGENT_ACTION_ADAPTER.validate_python(payload)
        assert parsed.action == payload["action"]
