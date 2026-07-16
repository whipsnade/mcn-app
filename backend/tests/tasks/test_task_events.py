from app.tasks.errors import canonical_platform, safe_error
from app.tasks.state import TaskEventType


def test_canonical_platform_uses_allowlist_and_hides_internal_tool_names() -> None:
    assert canonical_platform("datatap.xiaohongshu.kol.search.v1") == "小红书"
    assert canonical_platform("datatap.douyin.kol.search.v1") == "抖音"
    assert canonical_platform("internal-secret-tool") == "社媒平台"


def test_safe_error_maps_known_codes_and_drops_sensitive_detail() -> None:
    error = safe_error("upstream_error", "https://secret.example/token=abc")

    assert error.code == "upstream_error"
    assert error.message == "社媒数据服务暂时不可用，请稍后重试。"
    assert "https" not in error.message
    assert TaskEventType.TOOL_FAILED.value == "tool.failed"


def test_safe_error_falls_back_to_generic_message_for_unknown_code() -> None:
    error = safe_error("Exception", "raw traceback with authorization token")

    assert error.code == "task_failed"
    assert error.message == "分析任务执行失败，请稍后重试。"
    assert len(error.message) <= 120
