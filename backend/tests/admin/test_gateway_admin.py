import pytest


@pytest.mark.asyncio
async def test_gateway_admin_tenant_license_user_and_audit_are_safe(authed_client_factory) -> None:
    client, _admin = await authed_client_factory()
    created = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-create-1"},
        json={"slug": "gateway-admin-tenant", "name": "Gateway 管理测试"},
    )
    assert created.status_code == 201
    tenant = created.json()
    tenant_id = tenant["id"]
    assert tenant["active_license_id"]
    assert "secret" not in created.text.lower()

    listed = await client.get("/api/v1/admin/tenants")
    assert listed.status_code == 200
    assert any(item["id"] == tenant_id for item in listed.json()["items"])

    user = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/users",
        headers={"Idempotency-Key": "user-create-1"},
        json={"nickname": "租户成员", "phone": "13900009991", "role": "member"},
    )
    assert user.status_code == 201
    assert user.json()["role"] == "member"

    licenses = await client.get(f"/api/v1/admin/tenants/{tenant_id}/license")
    assert licenses.status_code == 200
    assert licenses.json()[0]["active"] is True
    appended = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/license",
        headers={"Idempotency-Key": "license-append-1"},
        json={
            "features": {"kol_selection": True, "brand_analysis": True},
            "max_concurrent_runs": 4,
            "max_user_concurrent_runs": 2,
        },
    )
    assert appended.status_code == 201
    activated = await client.patch(
        f"/api/v1/admin/tenants/{tenant_id}/license/{appended.json()['id']}",
        headers={"Idempotency-Key": "license-activate-1"},
        json={"status": "active"},
    )
    assert activated.status_code == 200

    usage = await client.get(f"/api/v1/admin/tenants/{tenant_id}/usage?group_by=day")
    assert usage.status_code == 200
    assert usage.json()["items"] == []


@pytest.mark.asyncio
async def test_gateway_admin_runtime_config_and_diagnostics_never_return_secrets(authed_client_factory) -> None:
    client, _admin = await authed_client_factory()
    tenant_response = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "runtime-admin-tenant-create"},
        json={"slug": "runtime-admin-tenant", "name": "Runtime 管理测试"},
    )
    tenant_id = tenant_response.json()["id"]
    config = await client.post(
        "/api/v1/admin/runtime-configs",
        headers={"Idempotency-Key": "runtime-config-1"},
        json={
            "tenant_id": tenant_id,
            "runtime_backend": "current",
            "model": {"name": "fake-model", "masked_origin": "fake"},
            "datatap": {"service": "fake", "schema_digest": "fake"},
            "limits": {"max_decisions": 5},
            "billing": {"mcp_call_points": 10},
        },
    )
    assert config.status_code == 201, config.text
    assert config.json()["secret_refs"] == []
    assert "sk-test" not in config.text
    listed = await client.get(f"/api/v1/admin/runtime-configs?tenant_id={tenant_id}")
    assert listed.status_code == 200
    missing = await client.get("/api/v1/admin/agent-runs/not-a-run/diagnostics")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_gateway_admin_rejects_non_admin(authed_client_factory) -> None:
    client, _user = await authed_client_factory(role="user", nickname="普通用户")
    response = await client.get("/api/v1/admin/tenants")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 真实幂等（Repair 4）：持久化唯一键 + 请求哈希 + 同事务响应投影
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_admin_writes_require_a_nonempty_idempotency_key(authed_client_factory) -> None:
    client, _admin = await authed_client_factory()
    response = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "no-key-tenant", "name": "缺幂等键"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "admin_idempotency_key_required"
    empty = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "  "},
        json={"slug": "blank-key-tenant", "name": "空幂等键"},
    )
    assert empty.status_code == 400


@pytest.mark.asyncio
async def test_same_key_same_request_replays_the_original_response(authed_client_factory, db_session) -> None:
    from app.admin.models import AdminAuditLog, AdminIdempotencyRecord
    from sqlalchemy import select

    client, admin = await authed_client_factory()
    payload = {"slug": "replay-tenant", "name": "幂等回放"}
    first = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-replay-1"},
        json=payload,
    )
    second = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-replay-1"},
        json=payload,
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()

    records = list(
        (
            await db_session.scalars(
                select(AdminIdempotencyRecord).where(
                    AdminIdempotencyRecord.actor_id == admin.id,
                    AdminIdempotencyRecord.action == "tenant.create",
                    AdminIdempotencyRecord.idempotency_key == "tenant-replay-1",
                )
            )
        ).all()
    )
    assert len(records) == 1
    audits = list(
        (
            await db_session.scalars(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_user_id == admin.id,
                    AdminAuditLog.action == "tenant.create",
                )
            )
        ).all()
    )
    assert len(audits) == 1  # 回放不产生第二笔业务写入或第二行审计


@pytest.mark.asyncio
async def test_same_key_different_request_returns_stable_409(authed_client_factory) -> None:
    client, _admin = await authed_client_factory()
    first = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-conflict-1"},
        json={"slug": "conflict-a", "name": "冲突 A"},
    )
    assert first.status_code == 201
    conflict = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-conflict-1"},
        json={"slug": "conflict-b", "name": "冲突 B"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "admin_idempotency_conflict"
    # 稳定可重读：相同 key + 原始请求仍返回首次结果
    replay = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "tenant-conflict-1"},
        json={"slug": "conflict-a", "name": "冲突 A"},
    )
    assert replay.status_code == 201
    assert replay.json() == first.json()


@pytest.mark.asyncio
async def test_license_append_retry_never_creates_a_second_version(authed_client_factory) -> None:
    client, _admin = await authed_client_factory()
    tenant = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "license-idem-tenant"},
        json={"slug": "license-idem-tenant", "name": "License 幂等"},
    )
    tenant_id = tenant.json()["id"]
    payload = {
        "features": {"kol_selection": True},
        "max_concurrent_runs": 4,
        "max_user_concurrent_runs": 2,
    }
    first = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/license",
        headers={"Idempotency-Key": "license-append-idem-1"},
        json=payload,
    )
    second = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/license",
        headers={"Idempotency-Key": "license-append-idem-1"},
        json=payload,
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    licenses = await client.get(f"/api/v1/admin/tenants/{tenant_id}/license")
    assert len(licenses.json()) == 2  # 初始 v1 + 追加 v2；重试未产生 v3


@pytest.mark.asyncio
async def test_runtime_config_retry_and_secret_never_enters_idempotency_projection(
    authed_client_factory, db_session, monkeypatch, request
) -> None:
    import base64

    from app.admin.models import AdminAuditLog, AdminIdempotencyRecord
    from sqlalchemy import select

    from app.core.config import get_settings

    monkeypatch.setenv(
        "RUNTIME_SECRET_MASTER_KEYS",
        "v1:" + base64.b64encode(b"t" * 32).decode("ascii"),
    )
    monkeypatch.setenv("RUNTIME_SECRET_ACTIVE_KEY_VERSION", "v1")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    client, admin = await authed_client_factory()
    tenant = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": "config-idem-tenant"},
        json={"slug": "config-idem-tenant", "name": "配置幂等"},
    )
    tenant_id = tenant.json()["id"]
    payload = {
        "tenant_id": tenant_id,
        "runtime_backend": "pi",
        "model": {"name": "fake-model", "masked_origin": "fake", "provider": "fake"},
        "datatap": {"service": "fake", "schema_digest": "fake"},
        "limits": {"max_decisions": 5},
        "billing": {"mcp_call_points": 10},
        "secrets": {
            "model_base_url": "http://model.invalid",
            "model_api_key": "unit-test-model-secret-7f3a",
            "datatap_token": "unit-test-datatap-secret-9b21",
            "datatap_urls": {"insight-cube": "http://127.0.0.1:9"},
        },
    }
    first = await client.post(
        "/api/v1/admin/runtime-configs",
        headers={"Idempotency-Key": "runtime-config-idem-1"},
        json=payload,
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/v1/admin/runtime-configs",
        headers={"Idempotency-Key": "runtime-config-idem-1"},
        json=payload,
    )
    assert second.status_code == 201
    assert second.json() == first.json()

    listed = await client.get(f"/api/v1/admin/runtime-configs?tenant_id={tenant_id}")
    assert listed.json()["total"] == 1  # 重试未创建第二个版本

    records = list(
        (
            await db_session.scalars(
                select(AdminIdempotencyRecord).where(
                    AdminIdempotencyRecord.actor_id == admin.id,
                    AdminIdempotencyRecord.idempotency_key == "runtime-config-idem-1",
                )
            )
        ).all()
    )
    assert len(records) == 1
    assert "unit-test-model-secret-7f3a" not in str(records[0].response_json)
    assert "unit-test-datatap-secret-9b21" not in str(records[0].response_json)
    audits = list(
        (
            await db_session.scalars(
                select(AdminAuditLog).where(AdminAuditLog.admin_user_id == admin.id)
            )
        ).all()
    )
    for row in audits:
        assert "unit-test-model-secret-7f3a" not in str(row.detail_json)
        assert "unit-test-datatap-secret-9b21" not in str(row.detail_json)
