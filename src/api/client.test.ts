import { afterEach, describe, expect, it, vi } from 'vitest';

import { authorizedFetch, setAccessToken } from './client';
import type { ApiUser } from './contracts';


describe('authorizedFetch', () => {
  afterEach(() => {
    setAccessToken(null);
    vi.unstubAllGlobals();
  });

  it('refreshes once after a 401 and retries with the new bearer token', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'fresh-token' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('expired-token');

    const response = await authorizedFetch('/api/v1/agent/runs/run-1/events');

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('does not retry the protected request if refresh fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);

    const response = await authorizedFetch('/api/v1/agent/runs/run-1/events');

    expect(response.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps tenant identity fields in the user contract', () => {
    const user: ApiUser = {
      id: 'user-1',
      nickname: '租户用户',
      role: 'user',
      channels: [],
      industries: ['美食'],
      tenant_id: 'tenant-1',
      tenant_name: '租户用户的个人租户',
      membership_role: 'owner',
    };

    expect(user.tenant_id).toBe('tenant-1');
    expect(user.membership_role).toBe('owner');
  });
});
