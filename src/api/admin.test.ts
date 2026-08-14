import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createAdminUser, deleteAdminUser, updateAdminUser } from './admin';
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

describe('admin api 幂等键', () => {
  beforeEach(() => {
    mockRequest.mockReset();
    mockRequest.mockResolvedValue(undefined as never);
  });

  it('createAdminUser 自动携带 Idempotency-Key', async () => {
    await createAdminUser({ nickname: '小A', phone: '13800000000', role: 'user' });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/users', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ nickname: '小A', phone: '13800000000', role: 'user' }),
    }));
    expect(lastHeaders()['Idempotency-Key']).toMatch(UUID_PATTERN);
  });

  it('createAdminUser 允许调用方显式覆盖幂等键', async () => {
    await createAdminUser({ nickname: '小A', phone: '13800000000', role: 'user' }, 'key-create-1');

    expect(lastHeaders()['Idempotency-Key']).toBe('key-create-1');
  });

  it('updateAdminUser 自动携带 Idempotency-Key', async () => {
    await updateAdminUser('u-1', { nickname: '小B' });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/users/u-1', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ nickname: '小B' }),
    }));
    expect(lastHeaders()['Idempotency-Key']).toMatch(UUID_PATTERN);
  });

  it('updateAdminUser 允许调用方显式覆盖幂等键', async () => {
    await updateAdminUser('u-1', { nickname: '小B' }, 'key-update-1');

    expect(lastHeaders()['Idempotency-Key']).toBe('key-update-1');
  });

  it('deleteAdminUser 自动携带 Idempotency-Key，且支持显式覆盖', async () => {
    await deleteAdminUser('u-1');
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/users/u-1', expect.objectContaining({
      method: 'DELETE',
    }));
    expect(lastHeaders()['Idempotency-Key']).toMatch(UUID_PATTERN);

    await deleteAdminUser('u-1', 'key-delete-1');
    expect(lastHeaders()['Idempotency-Key']).toBe('key-delete-1');
  });

  it('两次自动调用生成不同的幂等键', async () => {
    await deleteAdminUser('u-1');
    const first = lastHeaders()['Idempotency-Key'];
    await deleteAdminUser('u-1');
    const second = lastHeaders()['Idempotency-Key'];

    expect(first).not.toBe(second);
  });
});
