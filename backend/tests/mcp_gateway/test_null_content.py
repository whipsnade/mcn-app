import pytest

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.service import McpCallService, PreparedMcpInvocation
from app.mcp_gateway.transport import RemoteToolResult


class _NullContentTransport:
    def protocol_session_digest(self, service):
        return None

    async def call_tool(self, service, remote_name, arguments):
        return RemoteToolResult(
            structured_content=None,
            is_error=False,
            upstream_request_id=None,
        )


class _ErrorTextTransport:
    def protocol_session_digest(self, service):
        return None

    async def call_tool(self, service, remote_name, arguments):
        return RemoteToolResult(
            structured_content=None,
            is_error=True,
            upstream_request_id=None,
            error_text='Error executing tool x: 分析对象校验失败: 标签名称 "美妆" 不在列表中。建议使用 match_best_tag 工具获取标准标签。',
        )


class _NoopArgumentsLoader:
    async def load_arguments(self, *, task_id: str, plan_step_id: str):
        return {}


@pytest.mark.asyncio
async def test_null_content_is_a_settled_empty_result_not_a_validation_error(
    db_session,
) -> None:
    """DataTap 对“查询成功但无数据”返回 is_error=False + null content。"""
    service = McpCallService(
        db_session,
        _NullContentTransport(),
        arguments_loader=_NoopArgumentsLoader(),
    )
    invocation = PreparedMcpInvocation(
        DataTapService.INSIGHT_CUBE,
        "social_statistic_overview",
        {"name": "小米"},
        {"type": "object", "additionalProperties": False},
        None,
    )

    outcome = await service.invoke_prepared(invocation)

    assert outcome.status == "succeeded"
    assert outcome.validated_output is None


@pytest.mark.asyncio
async def test_upstream_error_text_is_carried_sanitized(db_session) -> None:
    service = McpCallService(
        db_session,
        _ErrorTextTransport(),
        arguments_loader=_NoopArgumentsLoader(),
    )
    invocation = PreparedMcpInvocation(
        DataTapService.INSIGHT_CUBE,
        "social_statistic_overview",
        {"name": "美妆"},
        {"type": "object", "additionalProperties": False},
        None,
    )

    outcome = await service.invoke_prepared(invocation)

    assert outcome.status == "failed"
    assert outcome.error_type == "upstream_tool_error"
    assert outcome.error_message is not None
    assert "match_best_tag" in outcome.error_message


def test_safe_upstream_text_redacts_urls_and_truncates() -> None:
    from app.mcp_gateway.service import safe_upstream_text

    assert safe_upstream_text(None) is None
    assert safe_upstream_text("   ") is None
    assert safe_upstream_text("见 https://internal.example.com 详情") is None
    assert safe_upstream_text("token=abc123 的配置错误") is None
    long_text = "错误" * 400
    assert len(safe_upstream_text(long_text)) == 300
