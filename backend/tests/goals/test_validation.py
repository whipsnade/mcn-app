import pytest

from app.goals.schemas import BrandSource, GoalParams, GoalPlannerOutput, GoalSpec
from app.goals.validation import GoalPlanSemanticError, validate_goal_plan


def _execute(
    *goals: GoalSpec,
    active_brand: str | None = None,
    brand_source: BrandSource = "none",
) -> GoalPlannerOutput:
    return GoalPlannerOutput(
        action="execute",
        goals=list(goals),
        active_brand=active_brand,
        brand_source=brand_source,
    )


def test_execute_rejects_duplicate_types_and_forward_dependency() -> None:
    duplicate = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            params=GoalParams(brand="喜茶"),
            request_evidence="分析喜茶",
        ),
        GoalSpec(
            sequence=2,
            goal_type="brand_analysis",
            params=GoalParams(brand="奈雪"),
            request_evidence="对比奈雪",
        ),
    )
    with pytest.raises(GoalPlanSemanticError, match="duplicate_goal_type"):
        validate_goal_plan(duplicate, "分析喜茶并对比奈雪")

    forward = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            depends_on_sequence=2,
            params=GoalParams(brand="喜茶"),
            request_evidence="分析喜茶",
        ),
        GoalSpec(
            sequence=2,
            goal_type="kol_selection",
            params=GoalParams(brand="喜茶"),
            request_evidence="圈选达人",
        ),
    )
    with pytest.raises(GoalPlanSemanticError, match="dependency_must_precede_goal"):
        validate_goal_plan(forward, "分析喜茶并圈选达人")


def test_campaign_requires_brand_and_campaign() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="campaign_analysis",
            params=GoalParams(brand="喜茶"),
            request_evidence="618 表现",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="campaign_scope_required"):
        validate_goal_plan(output, "分析喜茶 618 表现")


def test_brand_analysis_requires_brand() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            params=GoalParams(),
            request_evidence="分析品牌表现",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="brand_scope_required"):
        validate_goal_plan(output, "分析品牌表现")


def test_kol_selection_requires_exact_current_message_evidence() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="kol_selection",
            params=GoalParams(brand="喜茶"),
            request_evidence="帮我圈选达人",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="selection_evidence_not_in_message"):
        validate_goal_plan(output, "分析喜茶最近一个月表现")


def test_campaign_then_selection_is_valid() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="campaign_analysis",
            params=GoalParams(brand="喜茶", campaign="618"),
            request_evidence="分析喜茶 618 表现",
        ),
        GoalSpec(
            sequence=2,
            goal_type="kol_selection",
            depends_on_sequence=1,
            params=GoalParams(brand="喜茶", campaign="618"),
            request_evidence="圈选下一轮达人",
        ),
        active_brand="喜茶",
        brand_source="explicit",
    )
    validate_goal_plan(output, "分析喜茶 618 表现，并根据效果圈选下一轮达人")


@pytest.mark.parametrize(
    ("goal", "expected_code"),
    [
        (
            GoalSpec(
                sequence=1,
                goal_type="brand_analysis",
                params=GoalParams(brand="   "),
                request_evidence="分析品牌",
            ),
            "brand_scope_required",
        ),
        (
            GoalSpec(
                sequence=1,
                goal_type="campaign_analysis",
                params=GoalParams(brand="喜茶", campaign="\t "),
                request_evidence="分析活动",
            ),
            "campaign_scope_required",
        ),
    ],
)
def test_brand_scopes_reject_whitespace_only_values(
    goal: GoalSpec,
    expected_code: str,
) -> None:
    with pytest.raises(GoalPlanSemanticError, match=expected_code):
        validate_goal_plan(_execute(goal), "分析品牌活动")


@pytest.mark.parametrize(
    ("output", "current_message", "session_brand", "account_brand", "expected_code"),
    [
        (
            _execute(
                GoalSpec(
                    sequence=1,
                    goal_type="brand_analysis",
                    params=GoalParams(brand="奈雪"),
                    request_evidence="分析奈雪",
                ),
                active_brand="奈雪",
                brand_source="session",
            ),
            "继续分析",
            "喜茶",
            None,
            "brand_source_context_mismatch",
        ),
        (
            _execute(
                GoalSpec(
                    sequence=1,
                    goal_type="brand_analysis",
                    params=GoalParams(brand="喜茶"),
                    request_evidence="分析喜茶",
                ),
                active_brand="喜茶",
                brand_source="account",
            ),
            "继续分析",
            None,
            None,
            "brand_source_context_mismatch",
        ),
        (
            _execute(
                GoalSpec(
                    sequence=1,
                    goal_type="campaign_analysis",
                    params=GoalParams(brand="奈雪", campaign="618"),
                    request_evidence="分析喜茶 618",
                ),
                active_brand="喜茶",
                brand_source="explicit",
            ),
            "分析喜茶 618",
            None,
            None,
            "goal_brand_mismatch",
        ),
    ],
)
def test_brand_source_and_scoped_goal_must_match_real_context(
    output: GoalPlannerOutput,
    current_message: str,
    session_brand: str | None,
    account_brand: str | None,
    expected_code: str,
) -> None:
    with pytest.raises(GoalPlanSemanticError, match=expected_code):
        validate_goal_plan(
            output,
            current_message,
            session_brand=session_brand,
            account_default_brand=account_brand,
        )


@pytest.mark.parametrize("evidence", ["达人表现", "达人贡献"])
def test_kol_selection_rejects_analysis_only_evidence(evidence: str) -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="kol_selection",
            params=GoalParams(),
            request_evidence=evidence,
        )
    )

    with pytest.raises(GoalPlanSemanticError, match="selection_intent_not_explicit"):
        validate_goal_plan(output, f"分析活动中哪些{evidence}最好")


@pytest.mark.parametrize("evidence", ["推荐下一轮达人", "圈选下一轮达人"])
def test_kol_selection_accepts_explicit_selection_action(evidence: str) -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="kol_selection",
            params=GoalParams(),
            request_evidence=evidence,
        )
    )

    validate_goal_plan(output, f"分析活动表现，并{evidence}")
