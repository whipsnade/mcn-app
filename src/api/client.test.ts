import { afterEach, describe, expect, it, vi } from 'vitest';

import { authorizedFetch, setAccessToken } from './client';
import { downloadLatestSessionExport } from './tasks';


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

    const response = await authorizedFetch('/api/v1/tasks/task-1/events');

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('does not retry the protected request if refresh fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);

    const response = await authorizedFetch('/api/v1/tasks/task-1/events');

    expect(response.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('downloads the latest session export as a blob with the server filename', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('xlsx', {
      status: 200,
      headers: {
        'Content-Disposition': "attachment; filename*=UTF-8''%E7%A7%91%E9%A2%9C%E6%B0%8F.xlsx",
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      },
    }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    const result = await downloadLatestSessionExport('session-1');

    expect(result.filename).toBe('科颜氏.xlsx');
    expect(await result.blob.text()).toBe('xlsx');
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/sessions/session-1/exports/latest.xlsx', expect.objectContaining({ credentials: 'include' }));
  });
});
