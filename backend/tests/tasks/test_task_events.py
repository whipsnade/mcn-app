import pytest

from app.tasks.errors import canonical_platform, safe_error
from app.tasks.state import TaskEventType


def _session_payload():
    return {
        "brand": "任务隔离品牌",
        "campaign_name": "软删除",
        "platforms": ["xiaohongshu"],
        "category": "美妆",
        "target_audience": "年轻女性",
        "initial_query": "创建初始分析任务",
    }


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


@pytest.mark.asyncio
async def test_deleted_session_hides_task_create_retry_detail_cancel_and_events(
    auth_client_factory,
) -> None:
    client = await auth_client_factory("13500000021")
    created = await client.post("/api/v1/sessions", json=_session_payload())
    session_id = created.json()["id"]
    task_id = created.json()["latest_task"]["id"]

    assert (await client.delete(f"/api/v1/sessions/{session_id}")).status_code == 204

    assert (
        await client.post(
            f"/api/v1/sessions/{session_id}/tasks", json={"content": "重新分析"}
        )
    ).status_code == 404
    assert (await client.post(f"/api/v1/tasks/{task_id}/retry")).status_code == 404
    assert (await client.get(f"/api/v1/tasks/{task_id}")).status_code == 404
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel")).status_code == 404
    assert (await client.get(f"/api/v1/tasks/{task_id}/events")).status_code == 404
