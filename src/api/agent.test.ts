import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentRun, ApiAgentSession, ApiAgentUpload } from './agent';
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
  retryRun,
  sendMessage,
  uploadAgentFile,
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

  it('sends parent run, artifact version and upload references via options', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-2',
      session_id: 's1',
      message_id: 'm2',
      status: 'queued',
      reused: false,
    });

    await sendMessage('s1', '分析活动效果', {
      parentRunId: 'run-1',
      artifactVersionIds: ['version-1', 'version-2'],
      uploadIds: ['upload-1'],
    });
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({
        content: '分析活动效果',
        parent_run_id: 'run-1',
        artifact_version_ids: ['version-1', 'version-2'],
        upload_ids: ['upload-1'],
      }),
    });
  });

  it('sends the idempotency key header via options', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-3',
      session_id: 's1',
      message_id: 'm3',
      status: 'queued',
      reused: false,
    });

    await sendMessage('s1', '内容', { idempotencyKey: 'key-1' });
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({ content: '内容' }),
      headers: { 'Idempotency-Key': 'key-1' },
    });
  });

  it('keeps the legacy positional call compatible when options are absent', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-4',
      session_id: 's1',
      message_id: 'm4',
      status: 'queued',
      reused: false,
    });

    await sendMessage('s1', '只发文本');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({ content: '只发文本' }),
    });
  });

  it('maps a legacy string idempotency key to the Idempotency-Key header', async () => {
    // 历史签名 sendMessage(sessionId, content, idempotencyKey?: string)：
    // 字符串第三参数不得被当作 options 对象静默丢弃。
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-5',
      session_id: 's1',
      message_id: 'm5',
      status: 'queued',
      reused: false,
    });

    await sendMessage('s1', '内容', 'legacy-key');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({ content: '内容' }),
      headers: { 'Idempotency-Key': 'legacy-key' },
    });
  });

  it('keeps empty arrays out of the message body', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-6',
      session_id: 's1',
      message_id: 'm6',
      status: 'queued',
      reused: false,
    });

    await sendMessage('s1', '内容', { artifactVersionIds: [], uploadIds: [] });
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/messages', {
      method: 'POST',
      body: JSON.stringify({ content: '内容' }),
    });
  });

  it('uploads a parsed file and returns the upload DTO', async () => {
    const { authorizedFetch } = await import('./client');
    // fixture 显式标注 satisfies：status 必须是 'uploaded' | 'parsed' | 'failed'，
    // 防止被自动推断成普通 string。
    const upload = {
      id: 'upload-1',
      original_filename: '投放数据.csv',
      mime_type: 'text/csv',
      size_bytes: 1024,
      sha256: 'abc123',
      status: 'parsed',
      error_code: null,
      created_at: '2026-08-01T10:00:00',
      completed_at: '2026-08-01T10:00:01',
    } satisfies ApiAgentUpload;
    vi.mocked(authorizedFetch).mockResolvedValue(
      new Response(JSON.stringify(upload), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const file = new File(['csv'], '投放数据.csv', { type: 'text/csv' });
    const result = await uploadAgentFile('s1', file);

    expect(result).toEqual(upload);
    expect(result.status).toBe('parsed');
    const [url, init] = vi.mocked(authorizedFetch).mock.calls[0];
    expect(url).toBe('/api/v1/agent/sessions/s1/uploads');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeInstanceOf(FormData);
    // multipart boundary 由浏览器生成：不得手动设置 Content-Type。
    expect(init?.headers).toBeUndefined();
  });

  it('rejects upload statuses outside the backend union at type level', () => {
    // 后端 UploadRead 实际状态只有 uploaded/parsed/failed：非法字面量必须被
    // 类型系统拒绝（@ts-expect-error 在类型放开时会报 unused，防止假绿）。
    const parsed: ApiAgentUpload = {
      id: 'upload-1',
      original_filename: '投放数据.csv',
      mime_type: 'text/csv',
      size_bytes: 1024,
      sha256: 'abc123',
      status: 'parsed',
      error_code: null,
      created_at: '2026-08-01T10:00:00',
      completed_at: null,
    };
    // @ts-expect-error - status 只能是 'uploaded' | 'parsed' | 'failed'
    const invalid: ApiAgentUpload = { ...parsed, status: 'drafting' };
    expect(invalid.status).toBe('drafting');
  });

  it('retries a failed run and returns the new run response', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({
      run_id: 'run-new',
      session_id: 's1',
      message_id: 'm5',
      status: 'queued',
      reused: false,
    });

    const result = await retryRun('run-old');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/runs/run-old/retry', {
      method: 'POST',
    });
    expect(result.run_id).toBe('run-new');
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
