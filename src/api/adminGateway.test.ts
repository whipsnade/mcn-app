import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  adjustTenantWallet,
  getTenantWallet,
  listAdminTenantUsers,
  listTenantQuota,
  setTenantQuota,
} from './adminGateway';
import { request } from './client';

vi.mock('./client', () => ({
  request: vi.fn(),
}));

const mockRequest = vi.mocked(request);

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// 取最近一次调用的请求头，便于集中断言幂等键。
function lastHeaders(): Record<string, string> {
  const init = mockRequest.mock.calls.at(-1)?.[1] as { headers?: Record<string, string> } | undefined;
  return init?.headers ?? {};
}

describe('adminGateway 租户钱包与额度', () => {
  beforeEach(() => {
    mockRequest.mockReset();
    mockRequest.mockResolvedValue(undefined as never);
  });

  it('listAdminTenantUsers 拉取租户成员列表', async () => {
    await listAdminTenantUsers('tenant-1');

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/tenants/tenant-1/users?limit=200');
  });

  it('getTenantWallet 拉取租户钱包只读投影', async () => {
    await getTenantWallet('tenant-1');

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/tenants/tenant-1/wallet');
  });

  it('adjustTenantWallet 发送 POST 并自动携带 Idempotency-Key', async () => {
    await adjustTenantWallet('tenant-1', { user_id: 'u-1', delta: 500, reason: '补偿' });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/tenants/tenant-1/wallet/adjust', {
      method: 'POST',
      headers: { 'Idempotency-Key': expect.stringMatching(UUID_PATTERN) },
      body: JSON.stringify({ user_id: 'u-1', delta: 500, reason: '补偿' }),
    });
  });

  it('adjustTenantWallet 允许调用方显式覆盖幂等键', async () => {
    await adjustTenantWallet('tenant-1', { user_id: 'u-1', delta: -100, reason: '扣回' }, 'key-wallet-1');

    expect(lastHeaders()['Idempotency-Key']).toBe('key-wallet-1');
  });

  it('listTenantQuota 拉取周期额度列表', async () => {
    await listTenantQuota('tenant-1');

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/tenants/tenant-1/quota');
  });

  it('setTenantQuota 发送 PUT 并自动携带 Idempotency-Key', async () => {
    await setTenantQuota('tenant-1', 'u-1', { points_limit: 2000 });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/tenants/tenant-1/quota/u-1', {
      method: 'PUT',
      headers: { 'Idempotency-Key': expect.stringMatching(UUID_PATTERN) },
      body: JSON.stringify({ points_limit: 2000 }),
    });
  });

  it('setTenantQuota 允许调用方显式覆盖幂等键，且路径参数做编码', async () => {
    await setTenantQuota('tenant-1', 'u/特殊 1', { points_limit: 0 }, 'key-quota-1');

    expect(mockRequest).toHaveBeenCalledWith(
      `/api/v1/admin/tenants/tenant-1/quota/${encodeURIComponent('u/特殊 1')}`,
      expect.objectContaining({ method: 'PUT' }),
    );
    expect(lastHeaders()['Idempotency-Key']).toBe('key-quota-1');
  });
});
