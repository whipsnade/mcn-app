import pytest

from app.agent_runtime.state import (
    InvalidRunTransition,
    TERMINAL_RUN_STATUSES,
    RunStatus,
    ensure_transition,
)


def test_run_status_values_are_frozen() -> None:
    assert {item.value for item in RunStatus} == {
        "queued",
        "running",
        "reviewing",
        "clarification_requested",
        "paused",
        "completed",
        "failed",
        "cancelled",
    }


def test_terminal_run_statuses_are_completed_failed_cancelled() -> None:
    assert TERMINAL_RUN_STATUSES == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.RUNNING),  # self-loop: call_tool / draft update
        (RunStatus.RUNNING, RunStatus.CLARIFICATION_REQUESTED),  # ask_user
        (RunStatus.RUNNING, RunStatus.REVIEWING),  # submit_review
        (RunStatus.RUNNING, RunStatus.COMPLETED),  # complete，无正式产物
        (RunStatus.RUNNING, RunStatus.PAUSED),  # 30 分钟或 50 决策
        (RunStatus.RUNNING, RunStatus.CANCELLED),  # 用户取消
        (RunStatus.RUNNING, RunStatus.FAILED),  # 不可恢复系统错误
        (RunStatus.REVIEWING, RunStatus.RUNNING),  # revise（最多打回 2 次）
        (RunStatus.REVIEWING, RunStatus.COMPLETED),  # batch 全部 approve + 原子发布
        (RunStatus.REVIEWING, RunStatus.FAILED),  # reject / 第 3 次仍未 approve
        (RunStatus.PAUSED, RunStatus.RUNNING),  # 用户继续
    ],
)
def test_allowed_run_transitions(source: RunStatus, target: RunStatus) -> None:
    ensure_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.QUEUED, RunStatus.REVIEWING),
        (RunStatus.QUEUED, RunStatus.PAUSED),
        (RunStatus.QUEUED, RunStatus.COMPLETED),
        (RunStatus.QUEUED, RunStatus.CANCELLED),
        (RunStatus.PAUSED, RunStatus.COMPLETED),
        (RunStatus.PAUSED, RunStatus.CANCELLED),
        (RunStatus.REVIEWING, RunStatus.CANCELLED),
        (RunStatus.REVIEWING, RunStatus.PAUSED),
        (RunStatus.CLARIFICATION_REQUESTED, RunStatus.RUNNING),
        (RunStatus.CLARIFICATION_REQUESTED, RunStatus.COMPLETED),
    ],
)
def test_invalid_run_transitions_raise(source: RunStatus, target: RunStatus) -> None:
    with pytest.raises(InvalidRunTransition):
        ensure_transition(source, target)


@pytest.mark.parametrize("target", tuple(RunStatus))
def test_terminal_completed_rejects_all_transitions(target: RunStatus) -> None:
    with pytest.raises(InvalidRunTransition):
        ensure_transition(RunStatus.COMPLETED, target)


@pytest.mark.parametrize(
    "terminal",
    [
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    ],
)
def test_terminal_run_cannot_return_to_running(terminal: RunStatus) -> None:
    with pytest.raises(InvalidRunTransition):
        ensure_transition(terminal, RunStatus.RUNNING)
    assert terminal in TERMINAL_RUN_STATUSES


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("queued", RunStatus.RUNNING),
        ("not-a-status", RunStatus.RUNNING),
        (RunStatus.QUEUED, "running"),
        (RunStatus.QUEUED, "not-a-status"),
    ],
)
def test_transition_rejects_non_run_status_values(source: object, target: object) -> None:
    with pytest.raises(InvalidRunTransition):
        ensure_transition(source, target)  # type: ignore[arg-type]
