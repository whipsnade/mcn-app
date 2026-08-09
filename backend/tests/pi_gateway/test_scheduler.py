from datetime import datetime

from app.pi_gateway.scheduler import QueueTenant, PiRunScheduler


def test_fair_tenant_selection_does_not_starve_small_tenants() -> None:
    tenants = {
        "tenant-a": QueueTenant(
            tenant_id="tenant-a",
            queued_count=20,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 0),
            last_claimed_at=None,
        ),
        "tenant-b": QueueTenant(
            tenant_id="tenant-b",
            queued_count=2,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 1),
            last_claimed_at=None,
        ),
        "tenant-c": QueueTenant(
            tenant_id="tenant-c",
            queued_count=2,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 2),
            last_claimed_at=None,
        ),
    }

    claimed: list[str] = []
    for _ in range(6):
        tenant_id = PiRunScheduler.choose_fair_tenant(tenants)
        assert tenant_id is not None
        claimed.append(tenant_id)
        item = tenants[tenant_id]
        item.queued_count -= 1
        item.last_claimed_at = datetime(2026, 1, 1, 0, 1, len(claimed))

    assert claimed == ["tenant-a", "tenant-b", "tenant-c", "tenant-a", "tenant-b", "tenant-c"]


def test_fair_selection_uses_queue_time_then_tenant_id_for_equal_cursor() -> None:
    tenants = {
        "tenant-b": QueueTenant(
            tenant_id="tenant-b",
            queued_count=1,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 2),
            last_claimed_at=None,
        ),
        "tenant-a": QueueTenant(
            tenant_id="tenant-a",
            queued_count=1,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 1),
            last_claimed_at=None,
        ),
    }
    assert PiRunScheduler.choose_fair_tenant(tenants) == "tenant-a"
