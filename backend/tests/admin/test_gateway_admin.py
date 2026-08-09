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
