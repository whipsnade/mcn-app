import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { getSession, listSessions } from '../api/sessions';
import { createTask, getCandidates, getReport } from '../api/tasks';
import { useTaskStream } from './useTaskStream';
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

vi.mock('../api/tasks', () => ({
  createTask: vi.fn(),
  getCandidates: vi.fn(),
  getReport: vi.fn(),
}));

vi.mock('./useTaskStream', () => ({
  useTaskStream: vi.fn(),
}));


describe('useWorkspace', () => {
  beforeEach(() => {
    vi.mocked(listSessions).mockResolvedValue([session]);
    vi.mocked(getSession).mockResolvedValue(restoredSession);
    vi.mocked(useTaskStream).mockReturnValue(undefined);
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

  it('creates a pending analysis task and merges the user message immediately', async () => {
    vi.mocked(createTask).mockResolvedValue({
      id: 'task-1',
      session_id: 'session-1',
      status: 'pending',
      estimated_points: 0,
      error_code: null,
      latest_report_id: null,
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.appendMessage('帮我筛选美妆达人');
    });

    expect(createTask).toHaveBeenCalledWith('session-1', { content: '帮我筛选美妆达人' });
    expect(result.current.activeTaskId).toBe('task-1');
    expect(result.current.activeSession?.status).toBe('analyzing');
    expect(result.current.activeSession?.messages.at(-1)?.text).toBe('帮我筛选美妆达人');
  });

  it('restores the matching candidate version and BI report for a historical task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: {
        taskId: 'task-1',
        status: 'completed',
        candidateVersion: 2,
        reportId: 'report-2',
      },
    });
    vi.mocked(getCandidates).mockResolvedValue({
      task_id: 'task-1', version: 2, total: 1, items: [{
        id: 'candidate-1', kol_id: 'kol-1', platform: 'bilibili', platform_account_id: 'up-1',
        nickname: '小美', profile_url: null, rank: 1, total_score: 88,
        scores: { audience: 90 }, matched_conditions: [], risks: [], recommendation: '优先联系',
      }],
    });
    vi.mocked(getReport).mockResolvedValue({
      id: 'report-2', task_id: 'task-1', report_version: 1, candidate_version: 2,
      overview: {}, score_composition: [], audience_content_fit: {}, platform_distribution: [],
      budget_analysis: {}, comparison: [], risks: [], conclusion: '候选匹配度高', sources: [],
      generated_at: '2026-07-15T10:00:00Z',
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-1'));

    expect(result.current.activeSession?.biReport?.id).toBe('report-2');
    expect(getCandidates).toHaveBeenCalledWith('task-1');
    expect(getReport).toHaveBeenCalledWith('report-2');
  });
});
