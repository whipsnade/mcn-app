import json
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import Wallet
from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User, UserChannelPermission
from app.main import create_app
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.transport import RemoteToolResult
from app.quick.router import quick_transport
from app.quick.tags import clear_tag_cache


@pytest.fixture(autouse=True)
def _isolate_tag_cache() -> AsyncIterator[None]:
    clear_tag_cache()
    yield
    clear_tag_cache()


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_SEARCH_REQUEST = _schema(
    {
        "page": {"type": "integer"},
        "size": {"type": "integer"},
        "categoryMentionsTag": {"type": "array", "items": {"type": "string"}},
        "textContentWord": {"type": "string"},
    }
)
_SEARCH_SCHEMA = _schema(
    {"request": {"anyOf": [_SEARCH_REQUEST, {"type": "null"}]}}, required=["request"]
)

CATALOG_SCHEMAS: dict[str, tuple[str, dict]] = {
    "datatap.insight.match.best.tag.v1": (
        "insight-cube-mcp",
        _schema(
            {
                "tag_type": {"type": "string"},
                "tag_names": {"type": "array", "items": {"type": "string"}},
                "requirement_desc": {"type": ["string", "null"]},
            },
            required=["tag_type", "tag_names"],
        ),
    ),
    "datatap.insight.query.raw.posts.v1": (
        "insight-cube-mcp",
        _schema(
            {
                "target_type": {"type": "string"},
                "tag_type": {"type": ["string", "null"]},
                "name": {"type": ["string", "null"]},
                "anys": {
                    "type": ["array", "null"],
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "field_name": {"type": ["string", "null"]},
                "field_value": {"type": ["array", "null"], "items": {"type": "string"}},
                "datasource": {"type": "array", "items": {"type": "string"}},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "order_by": {"type": ["string", "null"]},
                "size": {"type": ["integer", "null"]},
            },
            required=["target_type", "datasource", "start_time", "end_time"],
        ),
    ),
    "datatap.social.grow.kol.match.mentions.tag.v1": (
        "social-grow-mcp",
        _schema(
            {
                "platform": {"type": "string"},
                "mentionsTagType": {"type": "integer"},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            required=["platform", "mentionsTagType", "keywords"],
        ),
    ),
    "datatap.social.grow.kol.detail.v1": (
        "social-grow-mcp",
        _schema(
            {
                "platform": {"type": "string"},
                "kwUidList": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "array", "items": {"type": "string"}},
                "startDate": {"type": ["string", "null"]},
                "endDate": {"type": ["string", "null"]},
            },
            required=["platform", "kwUidList", "scope"],
        ),
    ),
    "datatap.social.grow.kol.bilibili.search.v1": ("social-grow-mcp", _SEARCH_SCHEMA),
    "datatap.social.grow.kol.weibo.search.v1": ("social-grow-mcp", _SEARCH_SCHEMA),
    "datatap.social.grow.kol.wechat.search.v1": ("social-grow-mcp", _SEARCH_SCHEMA),
}

# 小红书/抖音搜索同时在静态 manifest 中登记：require_enabled 走 manifest 路径，
# 要求 catalog 行的 input_schema 与 discovery_digest 与清单完全一致。
_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "mcp_gateway" / "approved_tools.json"
)
_MANIFEST_SEARCH_TOOLS = (
    "datatap.xiaohongshu.kol.search.v1",
    "datatap.douyin.kol.search.v1",
)


def _manifest_entries() -> dict[str, dict]:
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return {tool["internal_name"]: tool for tool in manifest["tools"]}


MENTIONS_TAG_RESULT = {
    "标签匹配结果列表": [
        {"关键词": "美食", "泛化词 (中间结果，勿用)": ["美食"], "标签集合": ["品类提及--美食--美食其他"]}
    ]
}
BEST_TAG_RESULT = "已找到合适的标签: 美食"


@pytest_asyncio.fixture
async def quick_catalog(db_session: AsyncSession) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    manifest = _manifest_entries()
    rows: list[McpToolCatalog] = []
    for internal_name, (service_slug, schema) in CATALOG_SCHEMAS.items():
        rows.append(
            McpToolCatalog(
                id=str(uuid4()),
                service_slug=service_slug,
                internal_tool_name=internal_name,
                reviewed_description="测试工具",
                input_schema_json=schema,
                output_validator_version="datatap_result_v1",
                discovery_digest="0" * 64,
                review_status="approved",
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
    for internal_name in _MANIFEST_SEARCH_TOOLS:
        entry = manifest[internal_name]
        rows.append(
            McpToolCatalog(
                id=str(uuid4()),
                service_slug=entry["service"],
                internal_tool_name=internal_name,
                reviewed_description=entry["description"],
                input_schema_json=entry["input_schema"],
                output_validator_version="datatap_result_v1",
                discovery_digest=entry["discovery_digest"],
                review_status="approved",
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
    for row in rows:
        db_session.add(row)
    await db_session.flush()


class FakeTransport:
    """脚本化传输层：按 remote_name 返回预置载荷，记录每次调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        # remote_name -> dict/list（自动 json.dumps）| str（原样）| Exception（抛出）
        self.results: dict[str, Any] = {}
        self.default: Any = {}

    def protocol_session_digest(self, service: Any) -> None:
        return None

    async def list_tools(self, service: Any) -> tuple:
        return ()

    async def call_tool(self, service: Any, remote_name: str, arguments: dict) -> RemoteToolResult:
        self.calls.append((str(service), remote_name, dict(arguments)))
        outcome = self.results.get(remote_name, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(arguments)
        text = outcome if isinstance(outcome, str) else json.dumps(outcome, ensure_ascii=False)
        return RemoteToolResult(
            structured_content={"result": text},
            is_error=False,
            upstream_request_id=None,
        )

    def called_arguments(self, remote_name: str) -> list[dict]:
        return [arguments for _service, name, arguments in self.calls if name == remote_name]

    def call_count(self, remote_name: str) -> int:
        return len(self.called_arguments(remote_name))


@pytest_asyncio.fixture
async def quick_client_factory(
    db_session: AsyncSession, quick_catalog: None
) -> AsyncIterator[
    Callable[..., Coroutine[Any, Any, tuple[AsyncClient, User, FakeTransport]]]
]:
    """带行业属性与钱包的认证客户端；传输层为 FakeTransport。"""
    clients: list[AsyncClient] = []

    async def create(
        *,
        industries: tuple[str, ...] = ("美食",),
        balance: int = 1000,
        channels: tuple[str, ...] = (),
    ) -> tuple[AsyncClient, User, FakeTransport]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        transport = FakeTransport()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[quick_transport] = lambda: transport
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname="快捷用户",
            role="user",
            status="active",
            industries=list(industries),
            created_at=now,
            updated_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            Wallet(user_id=user.id, balance=balance, reserved=0, version=0, updated_at=now)
        )
        login_session = LoginSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=uuid4().hex + uuid4().hex,
            expires_at=now + timedelta(days=1),
            revoked_at=None,
            created_at=now,
            last_seen_at=now,
        )
        db_session.add(login_session)
        for channel in channels:
            db_session.add(
                UserChannelPermission(
                    id=str(uuid4()),
                    user_id=user.id,
                    channel=channel,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db_session.flush()
        token = create_access_token(user_id=user.id, session_id=login_session.id, role="user")
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        test_client.headers["Authorization"] = f"Bearer {token}"
        clients.append(test_client)
        return test_client, user, transport

    yield create
    for test_client in clients:
        await test_client.aclose()
