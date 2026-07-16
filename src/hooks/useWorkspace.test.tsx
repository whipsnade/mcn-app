import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { createSession, getSession, listSessions } from '../api/sessions';
import { createTask, getCandidates, getReport, retryTask } from '../api/tasks';
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
    taskId: 'task-1',
  }],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  return {
    promise: new Promise<T>(next => { resolve = next; }),
    resolve,
  };
}

function candidatePage(version: number, kolId: string) {
  return {
    task_id: 'task-1', version, total: 1, items: [{
      id: `candidate-${version}`, kol_id: kolId, platform: 'bilibili', platform_account_id: `up-${version}`,
      nickname: `达人${version}`, profile_url: null, rank: 1, total_score: 80 + version,
      scores: {}, matched_conditions: [], risks: [], recommendation: '优先联系',
    }],
  };
}

function report(version: number) {
  return {
    id: `report-${version}`, task_id: 'task-1', report_version: version, candidate_version: version,
    overview: {}, score_composition: [], audience_content_fit: {}, platform_distribution: [],
    budget_analysis: {}, comparison: [], risks: [], conclusion: `结论${version}`, sources: [],
    generated_at: '2026-07-15T10:00:00Z',
  };
}


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
  retryTask: vi.fn(),
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

  it('subscribes to the initial analysis task returned after creating a session', async () => {
    vi.mocked(createSession).mockResolvedValue({
      ...session,
      id: 'session-2',
      title: '测试品牌',
      campaignName: null,
      messages: [{
        id: 'message-2',
        sender: 'user',
        text: '新建会话后立即分析',
        timestamp: '18:01',
      }],
      analysis: { taskId: 'task-2', status: 'pending' },
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.createSession({
        brand: '测试品牌',
        campaign_name: null,
        platforms: ['xiaohongshu'],
        category: '美妆',
        target_audience: '一线城市女性',
        initial_query: '新建会话后立即分析',
      });
    });

    expect(result.current.activeTaskId).toBe('task-2');
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
    expect(result.current.isAnalyzing).toBe(true);
  });

  it('does not create a second task while the active task is still running', async () => {
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
      await result.current.appendMessage('先筛选美妆达人');
    });

    await expect(result.current.appendMessage('重复提交')).rejects.toThrow('TASK_IN_PROGRESS');
    expect(createTask).toHaveBeenCalledTimes(1);
  });

  it('keeps an interrupted task recoverable and blocks a new task request', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-interrupted', status: 'interrupted' },
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.analysis?.status).toBe('interrupted'));

    expect(result.current.isAnalyzing).toBe(true);
    await expect(result.current.appendMessage('不要新建')).rejects.toThrow('TASK_IN_PROGRESS');
    expect(createTask).not.toHaveBeenCalled();
  });

  it('rejects a second submission while the first task request is still pending', async () => {
    const pendingTask = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null; latest_report_id: null;
    }>();
    vi.mocked(createTask).mockReturnValueOnce(pendingTask.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let firstSubmission!: Promise<unknown>;
    act(() => {
      firstSubmission = result.current.appendMessage('首次提交');
    });
    await waitFor(() => expect(createTask).toHaveBeenCalledOnce());

    await expect(result.current.appendMessage('并发重复提交')).rejects.toThrow('TASK_IN_PROGRESS');
    expect(createTask).toHaveBeenCalledTimes(1);

    await act(async () => {
      pendingTask.resolve({
        id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0, error_code: null, latest_report_id: null,
      });
      await firstSubmission;
    });
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

  it('does not restore a BI report when the fetched candidate version has advanced', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'completed', candidateVersion: 2, reportId: 'report-2' },
    });
    vi.mocked(getCandidates).mockResolvedValue({ task_id: 'task-1', version: 3, total: 0, items: [] });
    vi.mocked(getReport).mockResolvedValue({
      id: 'report-2', task_id: 'task-1', report_version: 1, candidate_version: 2,
      overview: {}, score_composition: [], audience_content_fit: {}, platform_distribution: [],
      budget_analysis: {}, comparison: [], risks: [], conclusion: '旧结论', sources: [],
      generated_at: '2026-07-15T10:00:00Z',
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.candidates).toEqual([]));

    expect(result.current.activeSession?.analysis?.candidateVersion).toBe(3);
    expect(result.current.activeSession?.biReport).toBeUndefined();
  });

  it('drops late candidate and BI responses from a previous candidate version', async () => {
    const oldCandidates = deferred<ReturnType<typeof candidatePage>>();
    const newCandidates = deferred<ReturnType<typeof candidatePage>>();
    const oldReport = deferred<ReturnType<typeof report>>();
    const newReport = deferred<ReturnType<typeof report>>();
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(getCandidates)
      .mockReturnValueOnce(oldCandidates.promise)
      .mockReturnValueOnce(newCandidates.promise);
    vi.mocked(getReport)
      .mockReturnValueOnce(oldReport.promise)
      .mockReturnValueOnce(newReport.promise);

    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-1'));

    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', candidateVersion: 1,
      visibleReportId: 'report-1', connection: 'connected',
    });
    rerender();
    await waitFor(() => expect(getCandidates).toHaveBeenCalledTimes(1));

    await act(async () => {
      oldCandidates.resolve(candidatePage(1, 'kol-old'));
      oldReport.resolve(report(1));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-old'));

    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 2, assistantDraft: '', candidateVersion: 2,
      visibleReportId: 'report-2', connection: 'connected',
    });
    rerender();
    await waitFor(() => expect(getCandidates).toHaveBeenCalledTimes(2));
    expect(result.current.activeSession?.candidates).toBeUndefined();

    await act(async () => {
      newCandidates.resolve(candidatePage(2, 'kol-new'));
      newReport.resolve(report(2));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.activeSession?.biReport?.id).toBe('report-2'));

    expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-new');
    expect(result.current.activeSession?.biReport?.id).toBe('report-2');
  });

  it('retries a terminal message in the same session and clears old artifacts', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      messages: restoredSession.messages.map(message => ({ ...message, taskId: 'task-old' })),
      analysis: { taskId: 'task-old', status: 'failed', candidateVersion: 2, reportId: 'report-old' },
      candidates: [{
        id: 'candidate-old', kolId: 'kol-old', platform: 'douyin', platformAccountId: 'old', rank: 1,
        totalScore: 70, scores: {}, matchedConditions: [], risks: [], recommendation: '可考虑',
      }],
    });
    vi.mocked(retryTask).mockResolvedValue({
      id: 'task-new', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, error_message: null, latest_report_id: null,
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-old'));

    await act(async () => {
      await result.current.retryMessage('message-1');
    });

    expect(retryTask).toHaveBeenCalledWith('task-old');
    expect(result.current.activeTaskId).toBe('task-new');
    expect(result.current.activeSession?.analysis?.candidateVersion).toBeUndefined();
    expect(result.current.activeSession?.biReport).toBeUndefined();
    expect(result.current.activeSession?.messages[0]?.taskId).toBe('task-new');
  });

  it('adds a persisted task error message once when the terminal event arrives', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 2, assistantDraft: '', connection: 'closed',
      status: 'failed', phase: 'failed', phaseLabel: '分析失败',
      errorMessage: '分析任务执行失败，请稍后重试。', errorMessageId: 'error-1',
    });
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.messages).toHaveLength(2));
    rerender();
    expect(result.current.activeSession?.messages.filter(message => message.id === 'error-1')).toHaveLength(1);
  });
});
