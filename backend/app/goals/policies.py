"""GoalPolicy：按 goal_type 分派的轻量策略（阶段三 Task 2）。

只覆盖当前执行器需要的四件事：循环系统 prompt 选择、export_contract 注入、
圈选证据沉淀开关。复合编排与全量策略方法属阶段四。
"""

from dataclasses import dataclass

from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    BRAND_ANALYSIS_LOOP_PROMPT,
    CAMPAIGN_ANALYSIS_LOOP_PROMPT,
    PromptTemplate,
)


@dataclass(frozen=True)
class GoalPolicy:
    goal_type: str
    prompt: PromptTemplate
    inject_export_contract: bool = False
    ingest_enabled: bool = False

    def loop_system_prompt(self) -> str:
        return self.prompt.system


_POLICIES = {
    policy.goal_type: policy
    for policy in (
        GoalPolicy(
            goal_type="kol_selection",
            prompt=AGENT_LOOP_PROMPT,
            inject_export_contract=True,
            ingest_enabled=True,
        ),
        GoalPolicy(goal_type="brand_analysis", prompt=BRAND_ANALYSIS_LOOP_PROMPT),
        GoalPolicy(goal_type="campaign_analysis", prompt=CAMPAIGN_ANALYSIS_LOOP_PROMPT),
    )
}


def policy_for(goal_type: str) -> GoalPolicy:
    try:
        return _POLICIES[goal_type]
    except KeyError:
        raise ValueError(f"unknown_goal_type:{goal_type}") from None
