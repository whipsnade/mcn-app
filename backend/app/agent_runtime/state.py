from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEWING = "reviewing"
    CLARIFICATION_REQUESTED = "clarification_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNINGS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

# 用户取消是跨切面迁移：除终态外，任何非终态（queued/paused/reviewing/
# clarification/running）都必须允许被用户取消。spec §七 图只画了 running→cancelled
# 作为主流程；用户取消必须覆盖全部非终态，否则 cancel() 会抛出未处理的
# InvalidRunTransition，或 request_cancel 后 Run 永远停在带取消信号的中间态。
ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.RUNNING,  # self-loop: call_tool / draft update
        RunStatus.CLARIFICATION_REQUESTED,  # ask_user
        RunStatus.REVIEWING,  # （遗留）submit_review；新执行路径不再进入 reviewing
        RunStatus.COMPLETED,  # complete，无正式产物或全部发布成功
        RunStatus.COMPLETED_WITH_WARNINGS,  # complete 但发布/放弃存在失败项
        RunStatus.PAUSED,  # 30 分钟或 50 决策
        RunStatus.CANCELLED,  # 用户取消
        RunStatus.FAILED,  # 不可恢复系统错误
    },
    RunStatus.REVIEWING: {
        RunStatus.RUNNING,  # （遗留）revise（最多打回 2 次）
        RunStatus.COMPLETED,  # （遗留）batch 全部 approve + 原子发布
        RunStatus.FAILED,  # 历史 reviewing Run 收口（LEGACY_REVIEWING_UNSUPPORTED）
        RunStatus.CANCELLED,  # 用户取消
    },
    # ask_user 后本 Run 以 clarification_requested 结果完成，用户回答创建新 Run（parent_run_id）。
    RunStatus.CLARIFICATION_REQUESTED: {RunStatus.CANCELLED},
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED},  # 用户继续 / 用户取消
    RunStatus.COMPLETED: set(),
    RunStatus.COMPLETED_WITH_WARNINGS: set(),
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
