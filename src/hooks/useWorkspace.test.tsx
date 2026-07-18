import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { createSession, deleteSession, getSession, listSessions, updateSession } from '../api/sessions';
import { createTask, getAnalysisReport, getCandidates, getReport, getTask, retryFollowups, retryTask } from '../api/tasks';
import { initialTaskRuntime } from '../state/taskEvents';
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
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  listSessions: vi.fn(),
  updateSession: vi.fn(),
}));

vi.mock('../api/tasks', () => ({
  createTask: vi.fn(),
  getTask: vi.fn(),
  getCandidates: vi.fn(),
  getReport: vi.fn(),
  getAnalysisReport: vi.fn(),
  retryFollowups: vi.fn(),
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

  it('accepts follow-up events only for the active task and exposes the latest suggestions', async () => {
    const withTask = {
      ...restoredSession,
      analysis: { taskId: 'task-follow', status: 'completed', followupStatus: 'pending' as const, followupSuggestions: [] },
    };
    vi.mocked(getSession).mockResolvedValue(withTask);
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-follow', lastEventId: 2, assistantDraft: '', connection: 'closed', status: 'completed',
      followupStatus: 'completed',
      followupSuggestions: [{ title: '分析地域', prompt: '请分析浙江粉丝', rationale: '优化投放区域' }],
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysis?.followupSuggestions?.[0]?.prompt).toBe('请分析浙江粉丝'));
    expect(result.current.activeSession?.analysis?.taskId).toBe('task-follow');
  });

  it('polls persisted follow-up metadata after an SSE event that contains only a count', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-poll', status: 'completed', followupStatus: 'pending', followupSuggestions: [] },
    });
    vi.mocked(getTask).mockResolvedValue({
      id: 'task-poll', session_id: 'session-1', status: 'completed', estimated_points: 0,
      error_code: null, error_message: null, latest_report_id: null,
      followup_suggestions_status: 'completed',
      followup_suggestions: [{ title: '分析平台', prompt: '请比较平台表现', rationale: '确认渠道差异' }],
      followup_error: null,
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysis?.followupSuggestions?.[0]?.title).toBe('分析平台'));
    expect(getTask).toHaveBeenCalledWith('task-poll');
  });

  it('keeps restored suggestions when the initial SSE runtime has no follow-up event', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: {
        taskId: 'task-restored', status: 'completed', followupStatus: 'completed',
        followupSuggestions: [{ title: '恢复建议', prompt: '请恢复分析', rationale: '保留历史结果' }],
      },
    });
    const runtime = initialTaskRuntime('task-restored');
    runtime.status = 'completed';
    runtime.connection = 'closed';
    vi.mocked(useTaskStream).mockReturnValue(runtime);
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysis?.followupSuggestions?.[0]?.title).toBe('恢复建议'));
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

  it('deletes a non-active session without changing the current view', async () => {
    const otherSession = { ...session, id: 'session-2', title: '另一个会话' };
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.deleteSession('session-2');
    });

    expect(result.current.sessions.map(item => item.id)).toEqual(['session-1']);
    expect(result.current.activeSession?.id).toBe('session-1');
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('preserves live active-session updates when deleting a non-active session', async () => {
    const otherSession = { ...session, id: 'session-2', title: '另一个会话' };
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-live', status: 'running' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-live',
      lastEventId: 1,
      assistantDraft: '',
      connection: 'connected',
      status: 'completed',
    });
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.status).toBe('completed'));

    await act(async () => {
      await result.current.deleteSession('session-2');
    });

    expect(result.current.activeSession?.status).toBe('completed');
    expect(result.current.activeSession?.analysis?.status).toBe('completed');
  });

  it('selects and hydrates the most recent remaining session after deleting the active one', async () => {
    const otherSession = {
      ...session,
      id: 'session-2',
      title: '最近访问的剩余会话',
      analysis: { taskId: 'task-2', status: 'completed' },
    };
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockImplementation(async id => id === 'session-1' ? restoredSession : otherSession);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.deleteSession('session-1');
    });

    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-2'));
    expect(result.current.sessions.map(item => item.id)).toEqual(['session-2']);
    expect(result.current.activeTaskId).toBe('task-2');
    expect(getSession).toHaveBeenCalledWith('session-2');
  });

  it('clears all active state after deleting the last session', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'completed', candidateVersion: 1, reportId: 'report-1' },
      candidates: [{
        id: 'candidate-1', kolId: 'kol-1', platform: 'douyin', platformAccountId: 'account-1', rank: 1,
        totalScore: 90, scores: {}, matchedConditions: [], risks: [], recommendation: '推荐',
      }],
      biReport: report(1),
    });
    vi.mocked(getCandidates).mockResolvedValue(candidatePage(1, 'kol-1'));
    vi.mocked(getReport).mockResolvedValue(report(1));
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-1'));

    await act(async () => {
      await result.current.deleteSession('session-1');
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
    expect(result.current.activeSessionId).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
    expect(result.current.taskRuntime).toBeUndefined();
  });

  it('keeps the session list unchanged and rethrows when deletion fails', async () => {
    vi.mocked(deleteSession).mockRejectedValue(new Error('DELETE_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await expect(act(async () => {
      await result.current.deleteSession('session-1');
    })).rejects.toThrow('DELETE_FAILED');

    expect(result.current.sessions).toEqual([restoredSession]);
    expect(result.current.activeSession?.id).toBe('session-1');
  });

  it('does not reinsert a deleted session when an older update response arrives', async () => {
    const pendingUpdate = deferred<Session>();
    vi.mocked(updateSession).mockReturnValue(pendingUpdate.promise);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let updatePromise!: Promise<Session>;
    act(() => {
      updatePromise = result.current.updateSession('session-1', { title: '晚到的名称' });
    });
    await waitFor(() => expect(updateSession).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.deleteSession('session-1');
    });

    await act(async () => {
      pendingUpdate.resolve({ ...restoredSession, title: '晚到的名称' });
      await updatePromise;
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
  });

  it('ignores a late task creation response after its session is deleted', async () => {
    const pendingTask = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null; latest_report_id: null;
    }>();
    vi.mocked(createTask).mockReturnValue(pendingTask.promise);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let appendPromise!: Promise<unknown>;
    act(() => {
      appendPromise = result.current.appendMessage('删除前发起的任务');
    });
    await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.deleteSession('session-1');
    });

    await act(async () => {
      pendingTask.resolve({
        id: 'task-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      });
      await appendPromise;
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
  });

  it('releases only the deleted session task lock and keeps a newer session lock owned', async () => {
    const otherSession = { ...session, id: 'session-2', title: '会话 B' };
    const firstTask = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null; latest_report_id: null;
    }>();
    const secondTask = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null; latest_report_id: null;
    }>();
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockImplementation(async id => id === 'session-1' ? restoredSession : otherSession);
    vi.mocked(createTask)
      .mockReturnValueOnce(firstTask.promise)
      .mockReturnValueOnce(secondTask.promise);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let firstPromise!: Promise<unknown>;
    act(() => {
      firstPromise = result.current.appendMessage('会话 A 的在途任务');
    });
    await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.selectSession('session-2');
      await result.current.deleteSession('session-1');
    });

    let secondPromise!: Promise<unknown>;
    act(() => {
      secondPromise = result.current.appendMessage('会话 B 的在途任务');
      void secondPromise.catch(() => undefined);
    });
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(2));

    await act(async () => {
      firstTask.resolve({
        id: 'task-a', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      });
      await firstPromise;
    });
    await expect(result.current.appendMessage('会话 B 的重复任务')).rejects.toThrow('TASK_IN_PROGRESS');

    await act(async () => {
      secondTask.resolve({
        id: 'task-b', session_id: 'session-2', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      });
      await secondPromise;
    });
    expect(result.current.activeTaskId).toBe('task-b');
  });

  it('does not bind a late task creation response to a different active session', async () => {
    const otherSession = {
      ...session,
      id: 'session-2',
      title: '会话 B',
      analysis: { taskId: 'task-b', status: 'completed' },
    };
    const pendingTask = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null; latest_report_id: null;
    }>();
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockImplementation(async id => id === 'session-1' ? restoredSession : otherSession);
    vi.mocked(createTask).mockReturnValue(pendingTask.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let appendPromise!: Promise<unknown>;
    act(() => {
      appendPromise = result.current.appendMessage('会话 A 的任务');
    });
    await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.selectSession('session-2');
    });
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-b'));

    await act(async () => {
      pendingTask.resolve({
        id: 'task-a-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      });
      await appendPromise;
    });

    expect(result.current.activeSession?.id).toBe('session-2');
    expect(result.current.activeTaskId).toBe('task-b');
    expect(result.current.sessions.find(item => item.id === 'session-1')?.analysis?.taskId).toBe('task-a-late');
  });

  it('ignores a late retry response after its session is deleted', async () => {
    const pendingRetry = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null;
      error_message: null; latest_report_id: null;
    }>();
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-old', status: 'failed' },
      messages: restoredSession.messages.map(message => ({ ...message, taskId: 'task-old' })),
    });
    vi.mocked(retryTask).mockReturnValue(pendingRetry.promise);
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-old'));

    let retryPromise!: Promise<unknown>;
    act(() => {
      retryPromise = result.current.retryMessage('message-1');
    });
    await waitFor(() => expect(retryTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.deleteSession('session-1');
    });

    await act(async () => {
      pendingRetry.resolve({
        id: 'task-retry-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, error_message: null, latest_report_id: null,
      });
      await retryPromise;
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
  });

  it('releases a deleted session retry lock so the selected session can submit', async () => {
    const otherSession = { ...session, id: 'session-2', title: '会话 B' };
    const pendingRetry = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null;
      error_message: null; latest_report_id: null;
    }>();
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockImplementation(async id => id === 'session-1' ? {
      ...restoredSession,
      analysis: { taskId: 'task-old', status: 'failed' },
      messages: restoredSession.messages.map(message => ({ ...message, taskId: 'task-old' })),
    } : otherSession);
    vi.mocked(retryTask).mockReturnValue(pendingRetry.promise);
    vi.mocked(createTask).mockResolvedValue({
      id: 'task-b', session_id: 'session-2', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    });
    vi.mocked(deleteSession).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-old'));

    let retryPromise!: Promise<unknown>;
    act(() => {
      retryPromise = result.current.retryMessage('message-1');
    });
    await waitFor(() => expect(retryTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.selectSession('session-2');
    });
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-2'));
    await act(async () => {
      await result.current.deleteSession('session-1');
      await result.current.appendMessage('会话 B 可以提交');
    });

    expect(createTask).toHaveBeenCalledWith('session-2', { content: '会话 B 可以提交' });
    expect(result.current.activeTaskId).toBe('task-b');

    await act(async () => {
      pendingRetry.resolve({
        id: 'task-retry-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, error_message: null, latest_report_id: null,
      });
      await retryPromise;
    });
    expect(result.current.activeTaskId).toBe('task-b');
  });

  it('does not bind a late retry response to a different active session', async () => {
    const otherSession = {
      ...session,
      id: 'session-2',
      title: '会话 B',
      analysis: { taskId: 'task-b', status: 'completed' },
    };
    const pendingRetry = deferred<{
      id: string; session_id: string; status: 'pending'; estimated_points: number; error_code: null;
      error_message: null; latest_report_id: null;
    }>();
    vi.mocked(listSessions).mockResolvedValue([session, otherSession]);
    vi.mocked(getSession).mockImplementation(async id => id === 'session-1' ? {
      ...restoredSession,
      analysis: { taskId: 'task-old', status: 'failed' },
      messages: restoredSession.messages.map(message => ({ ...message, taskId: 'task-old' })),
    } : otherSession);
    vi.mocked(retryTask).mockReturnValue(pendingRetry.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-old'));

    let retryPromise!: Promise<unknown>;
    act(() => {
      retryPromise = result.current.retryMessage('message-1');
    });
    await waitFor(() => expect(retryTask).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.selectSession('session-2');
    });
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-b'));

    await act(async () => {
      pendingRetry.resolve({
        id: 'task-retry-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, error_message: null, latest_report_id: null,
      });
      await retryPromise;
    });

    expect(result.current.activeSession?.id).toBe('session-2');
    expect(result.current.activeTaskId).toBe('task-b');
  });

  it('allows an older update to apply when deletion fails', async () => {
    const pendingUpdate = deferred<Session>();
    vi.mocked(updateSession).mockReturnValue(pendingUpdate.promise);
    vi.mocked(deleteSession).mockRejectedValue(new Error('DELETE_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let updatePromise!: Promise<Session>;
    act(() => {
      updatePromise = result.current.updateSession('session-1', { title: '删除失败后保留' });
    });
    await waitFor(() => expect(updateSession).toHaveBeenCalledOnce());
    await expect(act(async () => {
      await result.current.deleteSession('session-1');
    })).rejects.toThrow('DELETE_FAILED');

    await act(async () => {
      pendingUpdate.resolve({ ...restoredSession, title: '删除失败后保留' });
      await updatePromise;
    });

    expect(result.current.activeSession?.title).toBe('删除失败后保留');
    expect(result.current.sessions).toHaveLength(1);
  });

  it('does not let an older detail response overwrite a newer selection', async () => {
    const second = { ...session, id: 'session-2', title: '第二个', analysis: { taskId: 'task-2', status: 'completed' } };
    const third = { ...session, id: 'session-3', title: '第三个', analysis: { taskId: 'task-3', status: 'completed' } };
    const oldDetail = deferred<Session>();
    vi.mocked(listSessions).mockResolvedValue([session, second, third]);
    vi.mocked(getSession).mockImplementation(id => {
      if (id === 'session-1') return Promise.resolve(restoredSession);
      if (id === 'session-2') return oldDetail.promise;
      return Promise.resolve(third);
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    act(() => { void result.current.selectSession('session-2'); });
    await waitFor(() => expect(getSession).toHaveBeenCalledWith('session-2'));
    await act(async () => { await result.current.selectSession('session-3'); });
    expect(result.current.activeSession?.id).toBe('session-3');
    expect(result.current.activeTaskId).toBe('task-3');

    await act(async () => { oldDetail.resolve(second); await oldDetail.promise; });

    expect(result.current.activeSession?.id).toBe('session-3');
    expect(result.current.activeTaskId).toBe('task-3');
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

  it('drops an advanced candidate response and does not restore its older BI report', async () => {
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
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.activeSession?.analysis?.candidateVersion).toBe(2);
    expect(result.current.activeSession?.candidates).toBeUndefined();
    expect(result.current.activeSession?.biReport).toBeUndefined();
  });

  it('drops historical candidates returned for another task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'completed', candidateVersion: 2 },
    });
    vi.mocked(getCandidates).mockResolvedValue({
      ...candidatePage(2, 'kol-wrong-task'),
      task_id: 'task-other',
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.activeSession?.analysis?.candidateVersion).toBe(2);
    expect(result.current.activeSession?.candidates).toBeUndefined();
  });

  it('drops a historical report returned for another task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'completed', candidateVersion: 2, reportId: 'report-2' },
    });
    vi.mocked(getCandidates).mockResolvedValue(candidatePage(2, 'kol-2'));
    vi.mocked(getReport).mockResolvedValue({ ...report(2), task_id: 'task-other' });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-2');
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

  it('keeps v2 candidates when the older v1 request returns last', async () => {
    const v1Candidates = deferred<ReturnType<typeof candidatePage>>();
    const v2Candidates = deferred<ReturnType<typeof candidatePage>>();
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(getCandidates)
      .mockReturnValueOnce(v1Candidates.promise)
      .mockReturnValueOnce(v2Candidates.promise);
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-1'));

    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected', candidateVersion: 1,
    });
    rerender();
    await waitFor(() => expect(getCandidates).toHaveBeenCalledTimes(1));

    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 2, assistantDraft: '', connection: 'connected', candidateVersion: 2,
    });
    rerender();
    await waitFor(() => expect(getCandidates).toHaveBeenCalledTimes(2));

    await act(async () => {
      v2Candidates.resolve(candidatePage(2, 'kol-v2'));
      await v2Candidates.promise;
    });
    await waitFor(() => expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-v2'));

    await act(async () => {
      v1Candidates.resolve(candidatePage(2, 'kol-v1-late'));
      await v1Candidates.promise;
    });

    expect(result.current.activeSession?.analysis?.candidateVersion).toBe(2);
    expect(result.current.activeSession?.candidates?.[0]?.kolId).toBe('kol-v2');
  });

  it('ignores a task runtime that does not belong to the active task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-current', status: 'running' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-stale', lastEventId: 1, assistantDraft: '', connection: 'connected', status: 'completed',
    });

    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-current'));

    expect(result.current.activeSession?.status).toBe('analyzing');
    expect(result.current.activeSession?.analysis?.status).toBe('running');
    expect(result.current.isAnalyzing).toBe(true);
    expect(result.current.taskRuntime).toBeUndefined();
  });

  it.each([
    ['wrong task', { ...candidatePage(1, 'kol-wrong-task'), task_id: 'task-other' }],
    ['wrong version', { ...candidatePage(2, 'kol-wrong-version'), task_id: 'task-1' }],
  ])('ignores a candidate response with %s metadata', async (_label, response) => {
    const pendingCandidates = deferred<ReturnType<typeof candidatePage>>();
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(getCandidates).mockReturnValue(pendingCandidates.promise);
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected', candidateVersion: 1,
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(getCandidates).toHaveBeenCalledWith('task-1'));

    await act(async () => {
      pendingCandidates.resolve(response);
      await pendingCandidates.promise;
    });

    expect(result.current.activeSession?.analysis?.candidateVersion).toBe(1);
    expect(result.current.activeSession?.candidates).toBeUndefined();
  });

  it('ignores a report response for another task', async () => {
    const pendingReport = deferred<ReturnType<typeof report>>();
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(getCandidates).mockResolvedValue(candidatePage(1, 'kol-1'));
    vi.mocked(getReport).mockReturnValue(pendingReport.promise);
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected',
      candidateVersion: 1, visibleReportId: 'report-1',
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(getReport).toHaveBeenCalledWith('report-1'));

    await act(async () => {
      pendingReport.resolve({ ...report(1), task_id: 'task-other' });
      await pendingReport.promise;
    });

    expect(result.current.activeSession?.biReport).toBeUndefined();
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

  it('retries suggestions for the same completed task without creating a new task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-failed-followup', status: 'completed', followupStatus: 'failed', followupSuggestions: [] },
    });
    vi.mocked(retryFollowups).mockResolvedValue({
      id: 'task-failed-followup', session_id: 'session-1', status: 'completed', estimated_points: 0,
      error_code: null, error_message: null, latest_report_id: null,
      // A stale read must not make the UI terminal again; 202 means retry started.
      followup_suggestions_status: 'failed', followup_suggestions: [], followup_error: null,
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.analysis?.followupStatus).toBe('failed'));

    await act(async () => await result.current.retryFollowups());

    expect(retryFollowups).toHaveBeenCalledWith('task-failed-followup');
    expect(createTask).not.toHaveBeenCalled();
    expect(result.current.activeSession?.analysis?.taskId).toBe('task-failed-followup');
    expect(result.current.activeSession?.analysis?.followupStatus).toBe('pending');
  });

  it('fetches the analysis report announced by report.updated for an agent task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running', kind: 'agent' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected',
      status: 'running', visibleAnalysisReportId: 'analysis-report-1',
    });
    vi.mocked(getAnalysisReport).mockResolvedValue({
      id: 'analysis-report-1', task_id: 'task-1', version: 1, title: '自由分析报告',
      blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-15T10:00:00Z',
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-1'));
    expect(getAnalysisReport).toHaveBeenCalledWith('analysis-report-1');
    expect(result.current.activeSession?.analysis?.analysisReportId).toBe('analysis-report-1');
  });

  it('ignores an analysis report response that belongs to another task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running', kind: 'agent' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected',
      status: 'running', visibleAnalysisReportId: 'analysis-report-1',
    });
    vi.mocked(getAnalysisReport).mockResolvedValue({
      id: 'analysis-report-1', task_id: 'task-other', version: 1, title: '自由分析报告',
      blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-15T10:00:00Z',
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(getAnalysisReport).toHaveBeenCalledWith('analysis-report-1'));
    expect(result.current.activeSession?.analysisReport).toBeUndefined();
  });

  it('restores the persisted analysis report for an agent session', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'completed', kind: 'agent', analysisReportId: 'analysis-report-1' },
    });
    vi.mocked(getAnalysisReport).mockResolvedValue({
      id: 'analysis-report-1', task_id: 'task-1', version: 1, title: '自由分析报告',
      blocks: [{ type: 'heading', text: '一、结论' }], conclusion: null, status: 'completed',
      generated_at: '2026-07-15T10:00:00Z',
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-1'));
    expect(getAnalysisReport).toHaveBeenCalledWith('analysis-report-1');
    expect(result.current.activeSession?.analysis?.kind).toBe('agent');
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
