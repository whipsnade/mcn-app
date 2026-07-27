import { afterEach, describe, expect, it, vi } from 'vitest';

import { createTask, createTurnId } from './tasks';
import type { TaskCreateResult } from './contracts';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

describe('tasks api', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it('creates a turn id with crypto.randomUUID when available', () => {
    const randomUUID = vi.fn().mockReturnValue('turn-uuid');
    vi.stubGlobal('crypto', { randomUUID });

    expect(createTurnId()).toBe('turn-uuid');
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it('creates a valid UUID v4 fallback turn id without crypto.randomUUID', () => {
    vi.stubGlobal('crypto', {});

    expect(createTurnId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it('uses crypto.getRandomValues for the fallback when available', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(7));
    vi.stubGlobal('crypto', { getRandomValues });

    expect(createTurnId()).toBe('07070707-0707-4707-8707-070707070707');
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it('returns the task outcome payload as-is', async () => {
    const { request } = await import('./client');
    const outcome: TaskCreateResult = {
      outcome: 'task',
      task: {
        id: 'task-1',
        session_id: 'session-1',
        status: 'pending',
        estimated_points: 0,
        error_code: null,
        latest_report_id: null,
      },
    };
    vi.mocked(request).mockResolvedValue(outcome);

    const result = await createTask(
      'session-1',
      { content: '圈选达人', turn_id: 'turn-1' },
      'key-1',
    );

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session-1/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'key-1' },
      body: JSON.stringify({ content: '圈选达人', turn_id: 'turn-1' }),
    });
    expect(result).toEqual(outcome);
    expect(result.outcome).toBe('task');
  });

  it('returns the clarify outcome payload as-is', async () => {
    const { request } = await import('./client');
    const outcome: TaskCreateResult = {
      outcome: 'clarify',
      message: {
        id: 'message-1',
        role: 'assistant',
        content: '想看哪个品牌？',
        sequence: 3,
        metadata: { clarify: { options: ['海底捞', '喜茶'] } },
        created_at: '2026-07-24T10:00:00',
      },
    };
    vi.mocked(request).mockResolvedValue(outcome);

    const result = await createTask('session-1', { content: '帮我分析' }, 'key-2');

    expect(result.outcome).toBe('clarify');
    if (result.outcome === 'clarify') {
      expect(result.message.metadata.clarify?.options).toEqual(['海底捞', '喜茶']);
    }
  });

  it('returns the respond outcome payload as-is', async () => {
    const { request } = await import('./client');
    const outcome: TaskCreateResult = {
      outcome: 'respond',
      respond_type: 'context_qa',
      message: {
        id: 'message-1',
        role: 'assistant',
        content: '上次失败是因为未采集到有效数据。',
        sequence: 4,
        metadata: { respond: { type: 'context_qa' } },
        created_at: '2026-07-27T00:00:00',
      },
    };
    vi.mocked(request).mockResolvedValue(outcome);

    const result = await createTask('session-1', { content: '为什么失败了？' }, 'key-3');

    expect(result).toEqual(outcome);
    if (result.outcome === 'respond') {
      expect(result.respond_type).toBe('context_qa');
      expect(result.message.content).toBe('上次失败是因为未采集到有效数据。');
    }
  });
});
