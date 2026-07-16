from __future__ import annotations

from dataclasses import dataclass


_PLATFORM_LABELS = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "weibo": "微博",
    "wechat": "微信",
}

_ERROR_MESSAGES = {
    "upstream_error": "社媒数据服务暂时不可用，请稍后重试。",
    "upstream_tool_error": "社媒数据服务返回错误，请稍后重试。",
    "output_validation_error": "社媒数据格式异常，暂时无法完成本轮分析。",
    "input_validation_error": "分析条件暂时无法提交，请检查筛选条件后重试。",
    "possibly_sent_timeout": "社媒数据服务响应超时，结果暂时无法确认。",
    "mcp_unknown_outcome": "社媒数据服务响应未确认，请稍后重试。",
    "mcp_call_failed": "社媒数据查询失败，请稍后重试。",
    "mcp_partial_failure": "部分社媒渠道查询失败，已保留可用结果。",
    "insufficient_balance": "积分余额不足，请充值后重试。",
    "task_cancelled": "分析任务已取消。",
    "model_error": "AI 分析服务暂时不可用，请稍后重试。",
    "task_failed": "分析任务执行失败，请稍后重试。",
}

_MODEL_ERROR_CODES = {
    "MODEL_PLAN_INVALID",
    "MODEL_STREAM_INTERRUPTED",
    "MODEL_REQUEST_FAILED",
}


@dataclass(frozen=True)
class SafeTaskError:
    code: str
    message: str


def canonical_platform(internal_tool_name: str | None) -> str:
    """将内部工具名转换为白名单中文平台名，不向外暴露工具标识。"""
    value = (internal_tool_name or "").casefold()
    for platform, label in _PLATFORM_LABELS.items():
        if platform in value:
            return label
    return "社媒平台"


def safe_error(code: str | None, _detail: object | None = None) -> SafeTaskError:
    """只返回白名单错误码和固定中文文案，绝不回显异常详情。"""
    normalized = (code or "").strip()
    if normalized in _MODEL_ERROR_CODES:
        normalized = "model_error"
    if normalized not in _ERROR_MESSAGES:
        normalized = "task_failed"
    return SafeTaskError(code=normalized, message=_ERROR_MESSAGES[normalized])


__all__ = ["SafeTaskError", "canonical_platform", "safe_error"]
