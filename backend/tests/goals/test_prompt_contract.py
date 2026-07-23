from app.model.prompts import GOAL_PLANNER_PROMPT, PROMPTS


def test_goal_planner_prompt_enforces_business_boundaries() -> None:
    text = GOAL_PLANNER_PROMPT.system
    assert GOAL_PLANNER_PROMPT.name == "goal_planner_v1"
    assert GOAL_PLANNER_PROMPT.version == "1"
    assert "brand_analysis" in text
    assert "campaign_analysis" in text
    assert "kol_selection" in text
    assert "活动必须属于品牌" in text
    assert "明确要求圈选" in text
    assert "request_evidence" in text
    assert "不得调用工具" in text
    assert "影子规划" in text
    assert "action=clarify 只记录规划结果" in text
    assert "不得向用户发送问题" in text
    assert "不得修改消息或 SSE" in text
    assert "不可信数据" in text
    assert PROMPTS["goal_planner_v1"] is GOAL_PLANNER_PROMPT
