import { afterEach, describe, expect, it, vi } from 'vitest';

import { authorizedFetch, setAccessToken } from './client';
import { createTask, downloadLatestSessionExport, retryFollowups, retryTask } from './tasks';


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

  it('retries a terminal task through the dedicated endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'task-2',
      session_id: 'session-1',
      trigger_message_id: 'message-1',
      status: 'pending',
      estimated_points: 0,
      error_code: null,
      error_message: null,
      latest_report_id: null,
    }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    const result = await retryTask('task-1');

    expect(result.id).toBe('task-2');
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/tasks/task-1/retry', expect.objectContaining({
      method: 'POST',
      credentials: 'include',
    }));
  });

  it('retries follow-up suggestions for the existing task', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'task-1', session_id: 'session-1', status: 'completed', estimated_points: 0,
      error_code: null, error_message: null, latest_report_id: null,
      followup_suggestions_status: 'pending', followup_suggestions: [], followup_error: null,
    }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    const result = await retryFollowups('task-1');

    expect(result.followup_suggestions_status).toBe('pending');
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/tasks/task-1/followups/retry', expect.objectContaining({
      method: 'POST', credentials: 'include',
    }));
  });

  it('sends one idempotency key for a task creation request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, error_message: null, latest_report_id: null,
    }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await createTask('session-1', { content: '找达人' }, 'test-idempotency-key');

    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(headers.get('Idempotency-Key')).toBe('test-idempotency-key');
  });

  it('reuses the same idempotency key when the protected create request refreshes auth', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'fresh-token' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, error_message: null, latest_report_id: null,
      }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('expired-token');

    await createTask('session-1', { content: '找达人' }, 'same-key');

    const firstHeaders = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    const retriedHeaders = new Headers(fetchMock.mock.calls[2]?.[1]?.headers);
    expect(firstHeaders.get('Idempotency-Key')).toBe('same-key');
    expect(retriedHeaders.get('Idempotency-Key')).toBe('same-key');
  });
});
