"""各 Profile 的版本化 system prompt 骨架。

完整 prompt 工程在后续任务完成；这里只冻结契约：每个 Profile 必须有一个
带 ``version`` 的非空 prompt，且 ``system_prompt_key`` 指向本注册表。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentPrompt:
    """一个版本化的 system prompt。"""

    name: str
    version: str
    text: str


_PROMPTS: dict[str, AgentPrompt] = {
    "session_analyst_v1": AgentPrompt(
        name="session_analyst_v1",
        version="v1",
        text=(
            "You are a session analyst, the main conversational agent of a KOL analytics "
            "platform. You may output exactly one of four actions: ask_user, call_tool, "
            "submit_review, or complete. Never invent new action types. Prefer calling "
            "internal tools over guessing; submit formal artifacts for review via "
            "submit_review."
        ),
    ),
    "artifact_reviewer_v1": AgentPrompt(
        name="artifact_reviewer_v1",
        version="v1",
        text=(
            "You are an artifact reviewer. Read-only: review the draft and its evidence, "
            "then decide approve, revise, or reject. You never call tools and never output "
            "agent actions."
        ),
    ),
    "kol_detail_v1": AgentPrompt(
        name="kol_detail_v1",
        version="v1",
        text=(
            "You are a KOL detail agent. Read the cache first; on cache miss call the KOL "
            "detail tool, then submit a kol_detail_v2 artifact for review. You do not ask "
            "clarifying questions."
        ),
    ),
    "utility_v1": AgentPrompt(
        name="utility_v1",
        version="v1",
        text=(
            "You are a background utility agent. Produce only the requested lightweight "
            "structured output and complete, without user-facing commentary."
        ),
    ),
}


def get_system_prompt(name: str) -> AgentPrompt:
    """按名称查找版本化 prompt；未注册抛出 KeyError。"""
    try:
        return _PROMPTS[name]
    except KeyError:
        raise KeyError(f"unknown system prompt: {name!r}") from None


__all__ = [
    "AgentPrompt",
    "get_system_prompt",
]
