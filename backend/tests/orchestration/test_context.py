from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.orchestration.context import ContextBuilder


class FakeWorkspace:
    async def get_owned_session(self, user_id: str, session_id: str):
        assert (user_id, session_id) == ("user-1", "session-1")
        return SimpleNamespace(
            id=session_id,
            brand="测试品牌",
            campaign_name="新品推广",
            platforms=["bilibili"],
            category="美妆",
            target_audience="18-30 岁学生",
            budget_min=1000,
            budget_max=5000,
            filters_snapshot={"followers_min": 10000},
        )

    async def list_messages(self, user_id: str, session_id: str):
        return [
            SimpleNamespace(role="user", content="请找合适的 UP 主", sequence=1),
            SimpleNamespace(role="assistant", content="我会先筛选", sequence=2),
        ]


class FakeRegistry:
    async def list_enabled(self):
        return (
            SimpleNamespace(
                catalog_id="catalog-1",
                internal_name="达人搜索",
                service=DataTapService.BILIBILI,
                remote_name="https://datatap.deepminer.com.cn/should-not-leak",
                input_schema={
                    "type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"result": {"type": "string"}},
                    "required": ["result"],
                },
            ),
        )


class FakePermissions:
    async def list_enabled_channels(self, user_id: str):
        assert user_id == "user-1"
        return ("bilibili",)


class LegacyChannelWorkspace(FakeWorkspace):
    async def get_owned_session(self, user_id: str, session_id: str):
        workspace = await super().get_owned_session(user_id, session_id)
        workspace.platforms = ["xiaohongshu", "douyin", "weibo"]
        return workspace


class LimitedPermissions:
    async def list_enabled_channels(self, user_id: str):
        assert user_id == "user-1"
        return ("xiaohongshu", "douyin")


class FakeReporting:
    async def context_summary(self, session_id: str):
        assert session_id == "session-1"
        return {
            "candidate_count": 0,
            "endpoint": "https://datatap.deepminer.com.cn/hidden",
            "nested": {
                "token": "hidden-token",
                "AuthorizationHeader": "hidden-authorization",
                "hostname": "internal.invalid",
                "credential_id": "hidden-credential",
                "safe_metric": 10,
                "disabled_service": "google-trends-mcp",
            },
            "items": [{"api_key": "hidden-key", "name": "安全候选"}],
            "evidence": "Authorization: Bearer secret-token",
            "notes": [
                {"note": "api key=secret-key"},
                {"note": "候选内容互动稳定"},
            ],
            "free_text": [
                "Authorization secret-auth-six",
                "Bearer: secret-bearer-seven",
                "api key secret-api-eight",
                "token secret-token-nine",
                "credential secret-credential-ten",
                "Authorization： secret-auth-eleven",
                "Bearer： secret-bearer-twelve",
                "api key： secret-api-thirteen",
                "token： secret-token-fourteen",
                "credentials： secret-credential-fifteen",
                "api　key： unicode-secret-a",
                "api＿key： unicode-secret-b",
                "api－key： unicode-secret-c",
                "api key： unicode-secret-d",
            ],
        }


@pytest.mark.asyncio
async def test_model_context_contains_reviewed_tools_but_no_supplier_details() -> None:
    builder = ContextBuilder(
        workspace=FakeWorkspace(),
        registry=FakeRegistry(),
        permissions=FakePermissions(),
        reporting=FakeReporting(),
    )

    context = await builder.build(user_id="user-1", session_id="session-1")

    serialized = context.model_dump_json()
    assert "达人搜索" in serialized
    assert context.tools[0].output_schema["properties"]["result"]["type"] == "string"
    assert "datatap.deepminer.com.cn" not in serialized
    assert "authorization" not in serialized.lower()
    assert "google-trends-mcp" not in serialized
    assert "hidden-token" not in serialized
    assert "hidden-key" not in serialized
    assert "hidden-authorization" not in serialized
    assert "hidden-credential" not in serialized
    assert "internal.invalid" not in serialized
    assert "secret-token" not in serialized
    assert "secret-key" not in serialized
    assert "secret-auth-six" not in serialized
    assert "secret-bearer-seven" not in serialized
    assert "secret-api-eight" not in serialized
    assert "secret-token-nine" not in serialized
    assert "secret-credential-ten" not in serialized
    assert "secret-auth-eleven" not in serialized
    assert "secret-bearer-twelve" not in serialized
    assert "secret-api-thirteen" not in serialized
    assert "secret-token-fourteen" not in serialized
    assert "secret-credential-fifteen" not in serialized
    assert "unicode-secret-a" not in serialized
    assert "unicode-secret-b" not in serialized
    assert "unicode-secret-c" not in serialized
    assert "unicode-secret-d" not in serialized
    assert "安全候选" in serialized
    assert "候选内容互动稳定" in serialized
    assert context.allowed_channels == ("bilibili",)


@pytest.mark.asyncio
async def test_context_ignores_legacy_unapproved_platforms() -> None:
    builder = ContextBuilder(
        workspace=LegacyChannelWorkspace(),
        registry=FakeRegistry(),
        permissions=LimitedPermissions(),
        reporting=FakeReporting(),
    )

    context = await builder.build(user_id="user-1", session_id="session-1")

    assert context.allowed_channels == ("xiaohongshu", "douyin")
    assert context.brief.platforms == ("xiaohongshu", "douyin")


@pytest.mark.asyncio
async def test_context_does_not_use_keyword_router_as_analysis_scope(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("keyword router must not decide planner scope")

    monkeypatch.setattr(
        "app.orchestration.context.classify_analysis_request",
        fail_if_called,
        raising=False,
    )

    builder = ContextBuilder(
        workspace=FakeWorkspace(),
        registry=FakeRegistry(),
        permissions=FakePermissions(),
        reporting=FakeReporting(),
    )

    context = await builder.build(user_id="user-1", session_id="session-1")

    assert context.analysis_scope is None
    assert context.analysis_objectives == ()
    # 未明确时间范围时默认最近三个月。
    assert context.requested_period["unit"] == "month"
    assert context.requested_period["value"] == 3
