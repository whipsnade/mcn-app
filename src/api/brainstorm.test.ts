import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { isBrainstormProfileReady, postBrainstorm } from './brainstorm';
import { request } from './client';


vi.mock('./client', () => ({ request: vi.fn() }));


const baseSession: Session = {
  id: 'session-1',
  title: '新会话1',
  brand: '',
  campaignName: null,
  status: 'draft',
  platform: '',
  category: '',
  targetAudience: '',
  summary: '',
  messages: [],
  isStarred: false,
  createdAt: '2026-07-20T10:00:00Z',
  updatedAt: '2026-07-20T10:00:00Z',
};


describe('postBrainstorm', () => {
  beforeEach(() => {
    vi.mocked(request).mockReset();
  });

  it('posts the content to the session brainstorm endpoint', async () => {
    const response = {
      ready: false,
      task_id: null,
      message: {
        id: 'm-1', role: 'assistant', content: '想分析哪个平台？', sequence: 2,
        metadata: { brainstorm: { ready: false, options: ['小红书'], profile_summary: null } },
        created_at: '2026-07-20T10:01:00Z',
      },
      profile: {
        brand: null, category: null, platforms: [], audience: null,
        period: null, kol_filters: null, goal: null,
      },
    };
    vi.mocked(request).mockResolvedValue(response);

    const result = await postBrainstorm('session-1', '想分析新品防晒');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session-1/brainstorm', {
      method: 'POST',
      body: JSON.stringify({ content: '想分析新品防晒' }),
    });
    expect(result).toBe(response);
  });
});


describe('isBrainstormProfileReady', () => {
  it('treats a session without brainstorm messages as not ready', () => {
    expect(isBrainstormProfileReady(baseSession)).toBe(false);
    expect(isBrainstormProfileReady({
      ...baseSession,
      messages: [{ id: 'm-1', sender: 'user', text: '想分析新品', timestamp: '10:00' }],
    })).toBe(false);
  });

  it('follows the latest message that carries brainstorm metadata', () => {
    const notReady = isBrainstormProfileReady({
      ...baseSession,
      messages: [
        { id: 'm-1', sender: 'ai', text: '信息已齐', timestamp: '10:00', brainstorm: { ready: true, options: [] } },
        { id: 'm-2', sender: 'ai', text: '继续追问', timestamp: '10:01', brainstorm: { ready: false, options: ['小红书'] } },
      ],
    });
    const ready = isBrainstormProfileReady({
      ...baseSession,
      messages: [
        { id: 'm-1', sender: 'ai', text: '想分析哪个平台？', timestamp: '10:00', brainstorm: { ready: false, options: ['小红书'] } },
        { id: 'm-2', sender: 'user', text: '小红书', timestamp: '10:01' },
        { id: 'm-3', sender: 'ai', text: '信息已齐，开始分析', timestamp: '10:02', brainstorm: { ready: true, options: [] } },
      ],
    });

    expect(notReady).toBe(false);
    expect(ready).toBe(true);
  });
});
