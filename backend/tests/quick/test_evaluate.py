import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import openpyxl
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import Wallet
from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app
from app.model.contracts import StructuredResult
from app.quick.models import QuickMcpCall
from app.quick.router import quick_model
from app.quick.service import MAX_UPLOAD_BYTES, EvaluateDocument


class FakeModel:
    def __init__(self, document: EvaluateDocument) -> None:
        self.document = document
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        return StructuredResult(
            value=self.document,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )

    def stream_text(self, request):  # pragma: no cover - 评估不走流式
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def evaluate_client_factory(db_session: AsyncSession):
    clients: list[AsyncClient] = []

    async def create(
        *, industries: tuple[str, ...] = ("美食",), balance: int = 1000
    ) -> tuple[AsyncClient, User, FakeModel]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        model = FakeModel(EvaluateDocument(title="火锅热度评估", analysis_markdown="# 结论\n热度上升"))
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[quick_model] = lambda: model
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname="评估用户",
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
        await db_session.flush()
        token = create_access_token(user_id=user.id, session_id=login_session.id, role="user")
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        test_client.headers["Authorization"] = f"Bearer {token}"
        clients.append(test_client)
        return test_client, user, model

    yield create
    for test_client in clients:
        await test_client.aclose()


def _xlsx_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["日期", "话题", "声量"])
    sheet.append(["2026-07-01", "火锅", 12345])
    sheet.append(["2026-07-02", "烧烤", 5432])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_evaluate_parses_xlsx_and_returns_model_document(
    evaluate_client_factory, db_session
) -> None:
    client, user, model = await evaluate_client_factory(balance=500)

    response = await client.post(
        "/api/v1/quick/evaluate",
        files={"file": ("热度数据.xlsx", _xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"title": "火锅热度评估", "analysis_markdown": "# 结论\n热度上升"}
    [request] = model.requests
    assert request.purpose == "quick_evaluate"
    assert request.template_name == "quick_evaluate_v1"
    user_message = request.messages[-1].content
    assert "火锅" in user_message
    assert "12345" in user_message
    assert "美食" in user_message  # 用户行业属性注入
    # 纯模型调用：不计费、不留 quick_mcp_calls 痕。
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 500
    assert wallet.reserved == 0
    assert (
        await db_session.scalar(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))
        is None
    )


@pytest.mark.asyncio
async def test_evaluate_accepts_csv(evaluate_client_factory) -> None:
    client, _user, model = await evaluate_client_factory()

    response = await client.post(
        "/api/v1/quick/evaluate",
        files={"file": ("data.csv", "日期,话题,声量\n2026-07-01,火锅,123\n".encode(), "text/csv")},
    )

    assert response.status_code == 200
    assert "火锅" in model.requests[-1].messages[-1].content


@pytest.mark.asyncio
async def test_evaluate_rejects_unsupported_extension(evaluate_client_factory) -> None:
    client, _user, _model = await evaluate_client_factory()

    response = await client.post(
        "/api/v1/quick/evaluate",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_evaluate_rejects_oversize_upload(evaluate_client_factory) -> None:
    client, _user, _model = await evaluate_client_factory()

    response = await client.post(
        "/api/v1/quick/evaluate",
        files={"file": ("big.csv", b"x" * (MAX_UPLOAD_BYTES + 1), "text/csv")},
    )

    assert response.status_code == 422
