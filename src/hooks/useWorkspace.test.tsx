import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { getSession, listSessions } from '../api/sessions';
import { useWorkspace } from './useWorkspace';


const session: Session = {
  id: 'session-1',
  title: '测试品牌-新品种草',
  brand: '测试品牌',
  campaignName: '新品种草',
  status: 'draft',
  platform: 'Xiaohongshu',
  category: '美妆',
  targetAudience: '一线城市女性',
  summary: '寻找合适的美妆达人',
  messages: [],
  isStarred: false,
  createdAt: '2026-07-14T10:00:00Z',
  updatedAt: '2026-07-14T10:00:00Z',
};

const restoredSession: Session = {
  ...session,
  messages: [{
    id: 'message-1',
    sender: 'user',
    text: '恢复这条历史提问',
    timestamp: '18:00',
  }],
};


vi.mock('../api/sessions', () => ({
  appendMessage: vi.fn(),
  createSession: vi.fn(),
  getSession: vi.fn(),
  listSessions: vi.fn(),
  updateSession: vi.fn(),
}));


describe('useWorkspace', () => {
  beforeEach(() => {
    vi.mocked(listSessions).mockResolvedValue([session]);
    vi.mocked(getSession).mockResolvedValue(restoredSession);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads persisted sessions and selects the first one after login', async () => {
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.sessions).toEqual([restoredSession]);
    expect(result.current.activeSession?.id).toBe('session-1');
    expect(result.current.activeSession?.messages[0]?.text).toBe('恢复这条历史提问');
  });

  it('clears the workspace when the user logs out', async () => {
    const { result, rerender } = renderHook(
      ({ userId }) => useWorkspace(userId),
      { initialProps: { userId: 'user-a' as string | undefined } },
    );

    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    await act(async () => rerender({ userId: undefined }));

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
  });

  it('ignores a previous user response that arrives after logout', async () => {
    let resolveList: (sessions: Session[]) => void = () => undefined;
    vi.mocked(listSessions).mockReturnValue(new Promise(resolve => {
      resolveList = resolve;
    }));

    const { result, rerender } = renderHook(
      ({ userId }) => useWorkspace(userId),
      { initialProps: { userId: 'user-a' as string | undefined } },
    );
    await waitFor(() => expect(listSessions).toHaveBeenCalledOnce());

    rerender({ userId: undefined });
    await act(async () => resolveList([session]));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
  });
});
