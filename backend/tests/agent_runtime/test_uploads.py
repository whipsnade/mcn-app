"""安全上传与 upload Evidence 测试（Gate B Task 3）。

覆盖：CSV/XLSX 上传解析为不可变 upload Evidence（XOR：tool_call_id 为
NULL、upload_id 有值）；xlsm/未知扩展名 415；超 20 MiB 413；超 50,000
数据行 400 + error_code；跨用户查询统一 404；GET 归属读取。
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent_runtime.models import EvidenceItem
from app.db.session import get_db
from app.main import create_app

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _csv_bytes(rows: list[list[object]]) -> bytes:
    return ("\n".join(",".join(str(cell) for cell in row) for row in rows) + "\n").encode(
        "utf-8"
    )


def _xlsx_bytes() -> bytes:
    """生成最小合法 xlsx（openpyxl 输出到内存）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["平台", "声量"])
    sheet.append(["小红书", 100])
    sheet.append(["抖音", 200])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def upload_client(db_session):
    """带鉴权头的测试客户端；上传服务注入临时目录。"""
    import tempfile

    from app.agent_runtime.router import get_upload_service

    temp_dir = tempfile.mkdtemp(prefix="agent-uploads-test-")
    clients: list[AsyncClient] = []

    async def _make(phone: str):
        app = create_app()

        async def _override_db():
            yield db_session

        async def _override_upload_service():
            from app.agent_runtime.uploads import UploadService

            yield UploadService(
                db_session,
                storage_dir=temp_dir,
                max_bytes=MAX_BYTES,
                max_rows=MAX_ROWS,
            )

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_upload_service] = _override_upload_service
        tc = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        login = await tc.post(
            "/api/v1/auth/mock/sms/login",
            json={"phone": phone, "code": "000000"},
        )
        assert login.status_code == 200, login.text
        tc.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        clients.append(tc)
        return tc, app

    yield _make
    for tc in clients:
        await tc.aclose()


async def _create_session(client: AsyncClient) -> str:
    resp = await client.post("/api/v1/agent/sessions", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_upload(db_session, user_id: str, session_id: str, **overrides) -> object:
    from app.agent_runtime.models import AgentUpload

    upload = AgentUpload(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session_id,
        original_filename="投放数据.csv",
        mime_type="text/csv",
        size_bytes=100,
        sha256="a" * 64,
        storage_key=f"{user_id}/{uuid4()}.csv",
        status="parsed",
        created_at=_now(),
        **overrides,
    )
    db_session.add(upload)
    await db_session.flush()
    return upload


async def _load_upload_evidence(db_session, upload_id: str) -> EvidenceItem | None:
    return await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.upload_id == upload_id)
    )


async def test_upload_csv_creates_upload_evidence(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000001")
    session_id = await _create_session(client)
    csv_bytes = _csv_bytes([["平台", "声量"], ["小红书", 100], ["抖音", 200]])

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("投放数据.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "parsed"
    assert body["original_filename"] == "投放数据.csv"
    assert body["mime_type"] == "text/csv"
    assert len(body["sha256"]) == 64

    evidence = await _load_upload_evidence(db_session, body["id"])
    assert evidence is not None
    assert evidence.source_type == "user_upload"
    assert evidence.tool_call_id is None
    assert evidence.upload_id == body["id"]
    assert evidence.raw_payload_json["rows"][0] == {"平台": "小红书", "声量": "100"}


async def test_upload_xlsx_creates_upload_evidence(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000002")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={
            "file": (
                "投放数据.xlsx",
                _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "parsed"
    evidence = await _load_upload_evidence(db_session, body["id"])
    assert evidence is not None
    assert evidence.source_type == "user_upload"
    assert evidence.tool_call_id is None


async def test_macro_workbook_is_rejected(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000003")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("投放数据.xlsm", b"PK\x03\x04fake", "application/vnd.ms-excel.sheet.macroEnabled.12")},
    )
    assert response.status_code == 415, response.text


async def test_upload_unknown_extension_is_rejected(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000004")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 415, response.text


async def test_upload_too_large_is_rejected(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000005")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("big.csv", b"x" * (MAX_BYTES + 1), "text/csv")},
    )
    assert response.status_code == 413, response.text


async def test_upload_over_rows_limit_is_failed(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000006")
    session_id = await _create_session(client)
    header = ["平台", "声量"]
    rows = [header] + [["平台", str(index)] for index in range(MAX_ROWS + 1)]
    csv_bytes = _csv_bytes(rows)

    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("超大.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "rows_exceeded"


async def test_upload_cross_user_is_404(upload_client, db_session) -> None:
    client_a, _app_a = await upload_client("13800000007")
    client_b, _app_b = await upload_client("13800000008")
    session_a = await _create_session(client_a)
    session_b = await _create_session(client_b)
    csv_bytes = _csv_bytes([["平台", "声量"], ["小红书", 100]])

    created = await client_a.post(
        f"/api/v1/agent/sessions/{session_a}/uploads",
        files={"file": ("投放数据.csv", csv_bytes, "text/csv")},
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]

    # B 用户读取 A 的上传 → 404；B 用户在自己的 Session 上传 → 正常。
    fetched = await client_b.get(f"/api/v1/agent/uploads/{upload_id}")
    assert fetched.status_code == 404, fetched.text
    own = await client_b.post(
        f"/api/v1/agent/sessions/{session_b}/uploads",
        files={"file": ("我的.csv", csv_bytes, "text/csv")},
    )
    assert own.status_code == 201, own.text


async def test_get_upload_owned(upload_client, db_session) -> None:
    client, _app = await upload_client("13800000009")
    session_id = await _create_session(client)
    csv_bytes = _csv_bytes([["平台", "声量"], ["小红书", 100]])

    created = await client.post(
        f"/api/v1/agent/sessions/{session_id}/uploads",
        files={"file": ("投放数据.csv", csv_bytes, "text/csv")},
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]

    fetched = await client.get(f"/api/v1/agent/uploads/{upload_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == upload_id
    assert fetched.json()["status"] == "parsed"
