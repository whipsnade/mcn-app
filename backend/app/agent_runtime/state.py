from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEWING = "reviewing"
    CLARIFICATION_REQUESTED = "clarification_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING},
    RunStatus.RUNNING: {
        RunStatus.RUNNING,  # self-loop: call_tool / draft update
        RunStatus.CLARIFICATION_REQUESTED,  # ask_user
        RunStatus.REVIEWING,  # submit_review
        RunStatus.COMPLETED,  # complete，无正式产物
        RunStatus.PAUSED,  # 30 分钟或 50 决策
        RunStatus.CANCELLED,  # 用户取消
        RunStatus.FAILED,  # 不可恢复系统错误
    },
    RunStatus.REVIEWING: {
        RunStatus.RUNNING,  # revise（最多打回 2 次）
        RunStatus.COMPLETED,  # batch 全部 approve + 原子发布
        RunStatus.FAILED,  # reject / 第 3 次仍未 approve
    },
    # ask_user 后本 Run 以 clarification_requested 结果完成，用户回答创建新 Run（parent_run_id）。
    RunStatus.CLARIFICATION_REQUESTED: set(),
    RunStatus.PAUSED: {RunStatus.RUNNING},  # 用户继续
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


class InvalidRunTransition(ValueError):
    pass


def ensure_transition(source: RunStatus, target: RunStatus) -> None:
    if not isinstance(source, RunStatus) or not isinstance(target, RunStatus):
        raise InvalidRunTransition(f"{source!r}->{target!r}")
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise InvalidRunTransition(f"{source.value}->{target.value}")
