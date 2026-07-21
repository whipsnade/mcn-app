"""快捷功能共享异常类型。"""

from __future__ import annotations


class QuickCallFailedError(RuntimeError):
    """快捷调用链任一环节失败；路由层映射为 502 QUICK_CALL_FAILED。"""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type
