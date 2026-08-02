import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentRun, ApiAgentSession } from './agent';
import {
  cancelRun,
  createKolDetailRun,
  createSession,
  deleteSession,
  fetchRunEvents,
  getSession,
  listSessions,
  patchSession,
  resumeRun,
  sendMessage,
} from './agent';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

const session: ApiAgentSession = {
  id: 's1',
  title: '新会话1',
  status: 'active',
  created_at: '2026-08-01T10:00:00',
  updated_at: '2026-08-01T10:00:00',
};

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('agent api', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it('creates a session with an optional title', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue(session);

    await expect(createSession('新会话1')).resolves.toEqual(session);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: '新会话1' }),
    });

    await createSession();
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions', {
      method: 'POST',
      body: JSON.stringify({}),
    });
  });

  it('lists sessions', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue([session]);

    await expect(listSessions()).resolves.toEqual([session]);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions');
  });

  it('fetches a session detail', async () => {
    const { request } = await import('./client');
    const detail = { ...session, messages: [], runs: [] };
    vi.mocked(request).mockResolvedValue(detail);

    await expect(getSession('s1')).resolves.toEqual(detail);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1');
  });

  it('patches a session title', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({ ...session, title: '改标题' });

    await patchSession('s1', { title: '改标题' });
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1', {
      method: 'PATCH',
      body: JSON.stringify({ title: '改标题' }),
    });
  });

  it('deletes a session', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue(undefined);

    await deleteSession('s1');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1', {
      method: 'DELETE',
    });
  });

  it('sends a message and returns the run id', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-1',
      session_id: 's1',
      message_id: 'm1',
      status: 'queued',
      reused: false,
    });

    const result = await sendMessage('s1', '帮我分析品牌');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({ content: '帮我分析品牌' }),
    });
    expect(result.run_id).toBe('run-1');
  });

  it('cancels and resumes a run', async () => {
    const { request } = await import('./client');
    const run: ApiAgentRun = {
      id: 'run-1',
      session_id: 's1',
      parent_run_id: null,
      profile_name: 'session_analyst_v1',
      status: 'queued',
      outcome: null,
      decision_count: 0,
      review_count: 0,
      revision_count: 0,
      error_code: null,
      started_at: null,
      paused_at: null,
      completed_at: null,
    };
    vi.mocked(request).mockResolvedValue(run);

    await cancelRun('run-1');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/runs/run-1/cancel', {
      method: 'POST',
    });

    await resumeRun('run-1');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/runs/run-1/resume', {
      method: 'POST',
    });
  });

  it('creates a kol-detail run with the selection ref payload', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-9',
      artifact_id: null,
      cached: false,
      detail: null,
    });

    await createKolDetailRun('s1', 'xiaohongshu', 'kol-1', {
      artifact_id: 'art-1',
      version: '3',
    });
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/kol-details', {
      method: 'POST',
      body: JSON.stringify({
        platform: 'xiaohongshu',
        kol_uid: 'kol-1',
        selection_artifact_id: 'art-1',
        selection_version: '3',
      }),
    });

    await createKolDetailRun('s1', 'douyin', 'kol-2');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/kol-details', {
      method: 'POST',
      body: JSON.stringify({
        platform: 'douyin',
        kol_uid: 'kol-2',
        selection_artifact_id: undefined,
        selection_version: undefined,
      }),
    });
  });

  it('streams run events as SSE and applies Last-Event-ID', async () => {
    const { authorizedFetch } = await import('./client');
    vi.mocked(authorizedFetch).mockResolvedValue(sseResponse([
      'id: 6\nevent: run.started\ndata: {"run_kind":"user"}\n\n',
      'id: 7\nevent: tool.started\ndata: {"internal_tool_name":"brand_search"}\n\n',
    ]));

    const onEvent = vi.fn();
    await fetchRunEvents('run-1', 5, new AbortController().signal, onEvent);

    const callArgs = vi.mocked(authorizedFetch).mock.calls[0];
    expect(callArgs[0]).toBe('/api/v1/agent/runs/run-1/events');
    const headers = callArgs[1]?.headers as Headers;
    expect(headers.get('Last-Event-ID')).toBe('5');
    expect(headers.get('Accept')).toBe('text/event-stream');
    expect(onEvent).toHaveBeenCalledTimes(2);
    expect(onEvent.mock.calls[0][0]).toEqual({
      id: 6,
      runId: 'run-1',
      type: 'run.started',
      payload: { run_kind: 'user' },
    });
    expect(onEvent.mock.calls[1][0]).toEqual({
      id: 7,
      runId: 'run-1',
      type: 'tool.started',
      payload: { internal_tool_name: 'brand_search' },
    });
  });
});
