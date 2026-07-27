import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import type { ApiBrainstormResponse } from '../api/contracts';
import { postBrainstorm } from '../api/brainstorm';
import { createSession, deleteSession, getSession, listSessions, updateSession } from '../api/sessions';
import {
  cancelTask,
  createTask,
  createTurnId,
  getAnalysisReport,
  getTask,
  retryFollowups,
  retryTask,
} from '../api/tasks';
import { getArtifactsSummary, markArtifactRead } from '../api/reports';
import type { ApiArtifactsSummary } from '../api/contracts';
import { initialTaskRuntime } from '../state/taskEvents';
import { useTaskStream } from './useTaskStream';
import type { TaskCreateResult } from '../api/contracts';


function taskOutcome(task: {
  id: string; session_id: string; status: string; estimated_points: number;
  error_code: null; latest_report_id: null;
}): TaskCreateResult {
  return { outcome: 'task', task: task as TaskCreateResult extends { task: infer T } ? T : never };
}
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

const readyBrainstormMessage: Session['messages'][number] = {
  id: 'message-brainstorm-ready',
  sender: 'ai',
  text: '信息已齐，开始分析',
  timestamp: '18:01',
  brainstorm: { ready: true, options: [] },
};

const restoredSession: Session = {
  ...session,
  messages: [{
    id: 'message-1',
    sender: 'user',
    text: '恢复这条历史提问',
    timestamp: '18:00',
    taskId: 'task-1',
  }, readyBrainstormMessage],
};

const blankSession: Session = {
  ...session,
  title: '新会话1',
  brand: '',
  campaignName: null,
  category: '',
  targetAudience: '',
  messages: [],
};

const emptyProfile: ApiBrainstormResponse['profile'] = {
  brand: null,
  category: null,
  platforms: [],
  audience: null,
  period: null,
  kol_filters: null,
  goal: null,
};

const emptyArtifactsSummary: ApiArtifactsSummary = {
  brand: { latest_artifact: null, unread: false },
  campaign: { latest_artifact: null, unread: false },
  kol_analysis: { latest_artifact: null, unread: false },
  kol_selection: { latest_artifact: null, unread: false },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  return {
    promise: new Promise<T>(next => { resolve = next; }),
    resolve,
  };
}


vi.mock('../api/sessions', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/sessions')>();
  return {
    ...actual,
    appendMessage: vi.fn(),
    createSession: vi.fn(),
    deleteSession: vi.fn(),
    getSession: vi.fn(),
    listSessions: vi.fn(),
    updateSession: vi.fn(),
  };
});

vi.mock('../api/brainstorm', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/brainstorm')>();
  return { ...actual, postBrainstorm: vi.fn() };
});

vi.mock('../api/tasks', () => ({
  cancelTask: vi.fn(),
  createTask: vi.fn(),
  createTurnId: vi.fn(),
  getTask: vi.fn(),
  getAnalysisReport: vi.fn(),
  retryFollowups: vi.fn(),
  retryTask: vi.fn(),
}));

vi.mock('../api/reports', () => ({
  getArtifactsSummary: vi.fn(),
  listSessionReports: vi.fn(),
  markArtifactRead: vi.fn(),
}));

vi.mock('./useTaskStream', () => ({
  useTaskStream: vi.fn(),
}));


describe('useWorkspace', () => {
  beforeEach(() => {
    vi.mocked(createTurnId).mockReturnValue('turn-1');
    vi.mocked(listSessions).mockResolvedValue([session]);
    vi.mocked(getSession).mockResolvedValue(restoredSession);
    vi.mocked(useTaskStream).mockReturnValue(undefined);
    vi.mocked(cancelTask).mockResolvedValue({
      id: 'task-1', session_id: 'session-1', status: 'running', estimated_points: 0,
      error_code: null, latest_report_id: null,
    });
    vi.mocked(getArtifactsSummary).mockResolvedValue(emptyArtifactsSummary);
    vi.mocked(markArtifactRead).mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads persisted sessions and selects the first one after login', async () => {
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.sessions).toEqual([{ ...restoredSession, artifactsSummary: emptyArtifactsSummary }]);
    expect(result.current.activeSession?.id).toBe('session-1');
    expect(result.current.activeSession?.messages[0]?.text).toBe('恢复这条历史提问');
  });

  it('keeps persisted terminal thinking metadata when restoring a session', async () => {
    const persistedThinking = {
      version: 1 as const,
      status: 'completed' as const,
      blocks: [{
        operationId: 'operation-1',
        purpose: 'agent_loop',
        attempt: 1,
        label: '正在分析数据',
        content: '先核对平台数据，再汇总结论。',
        status: 'completed' as const,
        startedAt: '2026-07-26T10:00:00Z',
        completedAt: '2026-07-26T10:00:01Z',
        durationMs: 1000,
        taskId: 'task-1',
        goalId: 'goal-1',
        truncated: false,
      }],
    };
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      messages: [
        restoredSession.messages[0],
        { ...readyBrainstormMessage, thinking: persistedThinking },
      ],
    });

    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(
      result.current.activeSession?.messages[1]?.thinking,
    ).toEqual(persistedThinking));
    expect(result.current.activeSession?.messages[1]?.thinking?.blocks[0]?.content)
      .toBe('先核对平台数据，再汇总结论。');
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

  it('clears the active task when the stream reports the task as not found', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-ghost', status: 'running' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-ghost',
      lastEventId: 0,
      assistantDraft: '',
      connection: 'closed',
      notFound: true,
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(useTaskStream).toHaveBeenCalledWith('task-ghost'));
    await waitFor(() => expect(result.current.activeTaskId).toBeUndefined());
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
      analysis: { taskId: 'task-1', status: 'completed', analysisReportId: 'analysis-report-1' },
    });
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

    expect(result.current.sessions).toEqual([{ ...restoredSession, artifactsSummary: emptyArtifactsSummary }]);
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
    const pendingTask = deferred<TaskCreateResult>();
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
      pendingTask.resolve(taskOutcome({
        id: 'task-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      }));
      await appendPromise;
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeSession).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
  });

  it('releases only the deleted session task lock and keeps a newer session lock owned', async () => {
    const otherSession = { ...session, id: 'session-2', title: '会话 B', messages: [readyBrainstormMessage] };
    const firstTask = deferred<TaskCreateResult>();
    const secondTask = deferred<TaskCreateResult>();
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
      firstTask.resolve(taskOutcome({
        id: 'task-a', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      }));
      await firstPromise;
    });
    await expect(result.current.appendMessage('会话 B 的重复任务')).rejects.toThrow('TASK_IN_PROGRESS');

    await act(async () => {
      secondTask.resolve(taskOutcome({
        id: 'task-b', session_id: 'session-2', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      }));
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
    const pendingTask = deferred<TaskCreateResult>();
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
      pendingTask.resolve(taskOutcome({
        id: 'task-a-late', session_id: 'session-1', status: 'pending', estimated_points: 0,
        error_code: null, latest_report_id: null,
      }));
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
    const otherSession = { ...session, id: 'session-2', title: '会话 B', messages: [readyBrainstormMessage] };
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
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-b', session_id: 'session-2', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
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

    expect(createTask).toHaveBeenCalledWith(
      'session-2',
      { content: '会话 B 可以提交', turn_id: 'turn-1' },
    );
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

  it('renders planner clarify response as an assistant message without creating a task', async () => {
    vi.mocked(createTask).mockResolvedValue({
      outcome: 'clarify',
      message: {
        id: 'message-clarify-1',
        role: 'assistant',
        content: '想看哪个品牌的分析？',
        sequence: 3,
        metadata: { clarify: { options: ['海底捞', '喜茶'] } },
        created_at: '2026-07-24T10:00:00Z',
      },
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.appendMessage('帮我做个分析');
    });

    expect(createTask).toHaveBeenCalledWith(
      'session-1',
      { content: '帮我做个分析', turn_id: 'turn-1' },
    );
    const active = result.current.activeSession;
    // 不落任务：状态与 analysis 不变，不进入 analyzing。
    expect(active?.status).toBe('draft');
    expect(active?.analysis).toBeUndefined();
    const texts = active?.messages.map(message => [message.sender, message.text]);
    expect(texts?.at(-2)).toEqual(['user', '帮我做个分析']);
    expect(texts?.at(-1)).toEqual(['ai', '想看哪个品牌的分析？']);
    expect(active?.messages.filter(message => message.text === '帮我做个分析')).toHaveLength(1);
    expect(active?.messages.at(-2)?.turnId).toBe('turn-1');
    expect(active?.messages.at(-1)?.clarify?.options).toEqual(['海底捞', '喜茶']);
  });

  it('renders planner respond outcome as an assistant message without creating a task', async () => {
    vi.mocked(createTask).mockResolvedValue({
      outcome: 'respond',
      respond_type: 'usage_help',
      message: {
        id: 'message-respond-1',
        role: 'assistant',
        content: '我可以帮你圈选达人、分析品牌或活动。',
        sequence: 3,
        metadata: { respond: { type: 'usage_help' } },
        created_at: '2026-07-27T10:00:00Z',
      },
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.appendMessage('你能做什么？');
    });

    expect(createTask).toHaveBeenCalledWith(
      'session-1',
      { content: '你能做什么？', turn_id: 'turn-1' },
    );
    const active = result.current.activeSession;
    // 不落任务：状态与 analysis 不变，不进入 analyzing，也不设置 activeTaskId。
    expect(active?.status).toBe('draft');
    expect(active?.analysis).toBeUndefined();
    expect(result.current.activeTaskId).toBeUndefined();
    const texts = active?.messages.map(message => [message.sender, message.text]);
    expect(texts?.at(-2)).toEqual(['user', '你能做什么？']);
    expect(texts?.at(-1)).toEqual(['ai', '我可以帮你圈选达人、分析品牌或活动。']);
    expect(active?.messages.filter(message => message.text === '你能做什么？')).toHaveLength(1);
    expect(active?.messages.at(-2)?.turnId).toBe('turn-1');
  });

  it('creates a pending analysis task and merges the user message immediately', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1',
      session_id: 'session-1',
      status: 'pending',
      estimated_points: 0,
      error_code: null,
      latest_report_id: null,
    }));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.appendMessage('帮我筛选美妆达人');
    });

    expect(createTask).toHaveBeenCalledWith(
      'session-1',
      { content: '帮我筛选美妆达人', turn_id: 'turn-1' },
    );
    expect(result.current.activeTaskId).toBe('task-1');
    expect(result.current.activeSession?.status).toBe('analyzing');
    expect(result.current.activeSession?.messages.at(-1)?.text).toBe('帮我筛选美妆达人');
    expect(result.current.isAnalyzing).toBe(true);
  });

  it('keeps a session-level analysis report when starting a new task', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysisReport: {
        id: 'analysis-report-kol', task_id: null, version: 1, title: 'KOL 匹配度分析',
        blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-21T10:00:00Z',
      },
    });
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-new', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-kol'));

    await act(async () => {
      await result.current.appendMessage('再跑一轮分析');
    });

    expect(result.current.activeTaskId).toBe('task-new');
    expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-kol');
  });

  it('clarifies an unready blank session through brainstorm instead of creating a task', async () => {
    vi.mocked(getSession).mockResolvedValue(blankSession);
    const pending = deferred<ApiBrainstormResponse>();
    vi.mocked(postBrainstorm).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let appendPromise!: Promise<unknown>;
    act(() => {
      appendPromise = result.current.appendMessage('想分析新品防晒');
    });
    await waitFor(() => expect(postBrainstorm).toHaveBeenCalledWith(
      'session-1',
      '想分析新品防晒',
      'turn-1',
    ));
    // 澄清进行中：本地已追加 user 消息并暴露 clarifying 状态，不创建任务。
    expect(result.current.isClarifying).toBe(true);
    expect(result.current.activeSession?.messages.at(-1)?.text).toBe('想分析新品防晒');
    expect(result.current.activeSession?.messages.at(-1)?.turnId).toBe('turn-1');
    expect(createTask).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve({
        ready: false,
        task_id: null,
        message: {
          id: 'm-assistant-1', role: 'assistant', content: '想分析哪个平台？', sequence: 2,
          metadata: { brainstorm: { ready: false, options: ['小红书', '抖音'], profile_summary: null } },
          created_at: '2026-07-20T10:01:00Z',
        },
        profile: emptyProfile,
      });
      await appendPromise;
    });

    expect(result.current.isClarifying).toBe(false);
    expect(result.current.activeSession?.status).toBe('draft');
    expect(result.current.activeTaskId).toBeUndefined();
    const messages = result.current.activeSession?.messages ?? [];
    expect(messages).toHaveLength(2);
    expect(messages[0]?.sender).toBe('user');
    expect(messages[1]?.text).toBe('想分析哪个平台？');
    expect(messages[1]?.brainstorm?.options).toEqual(['小红书', '抖音']);
    expect(createTask).not.toHaveBeenCalled();
  });

  it('binds the created task when brainstorm reports ready', async () => {
    vi.mocked(getSession).mockResolvedValue(blankSession);
    vi.mocked(postBrainstorm).mockResolvedValue({
      ready: true,
      task_id: 'task-bs',
      message: {
        id: 'm-assistant-ready', role: 'assistant', content: '信息已齐，开始分析', sequence: 2,
        metadata: { brainstorm: { ready: true, options: [], profile_summary: null } },
        created_at: '2026-07-20T10:02:00Z',
      },
      profile: { ...emptyProfile, brand: '新品防晒' },
    });
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await result.current.appendMessage('分析小红书上新品防晒的声量');
    });

    expect(createTask).not.toHaveBeenCalled();
    expect(result.current.activeTaskId).toBe('task-bs');
    expect(result.current.activeSession?.status).toBe('analyzing');
    expect(result.current.activeSession?.analysis).toEqual({ taskId: 'task-bs', status: 'pending', kind: 'agent' });
    const messages = result.current.activeSession?.messages ?? [];
    expect(messages[0]?.taskId).toBe('task-bs');
    expect(messages[1]?.brainstorm?.ready).toBe(true);
    expect(result.current.isAnalyzing).toBe(true);
  });

  it('rolls back the optimistic message when brainstorm fails', async () => {
    vi.mocked(getSession).mockResolvedValue(blankSession);
    vi.mocked(postBrainstorm).mockRejectedValue(new Error('BRAINSTORM_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let caught: unknown;
    await act(async () => {
      await result.current.appendMessage('想分析新品防晒').catch(reason => { caught = reason; });
    });

    expect((caught as Error)?.message).toBe('BRAINSTORM_FAILED');
    expect(result.current.activeSession?.messages).toHaveLength(0);
    expect(result.current.error).toBe('BRAINSTORM_FAILED');
    expect(result.current.isClarifying).toBe(false);
    expect(createTask).not.toHaveBeenCalled();
  });

  it('inserts a turn-linked task message before the request settles and replaces its id on success', async () => {
    const pending = deferred<TaskCreateResult>();
    vi.mocked(createTask).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    let appendPromise!: Promise<unknown>;
    act(() => {
      appendPromise = result.current.appendMessage('分析品牌');
    });

    await waitFor(() => expect(createTask).toHaveBeenCalledWith(
      'session-1',
      { content: '分析品牌', turn_id: 'turn-1' },
    ));
    expect(result.current.activeSession?.messages.at(-1)).toMatchObject({
      sender: 'user',
      text: '分析品牌',
      turnId: 'turn-1',
    });

    await act(async () => {
      pending.resolve({
        outcome: 'task',
        task: {
          id: 'task-turn',
          session_id: 'session-1',
          trigger_message_id: 'message-trigger',
          status: 'pending',
          estimated_points: 0,
          error_code: null,
          latest_report_id: null,
        },
      });
      await appendPromise;
    });

    expect(result.current.activeSession?.messages.at(-1)).toMatchObject({
      id: 'message-trigger',
      taskId: 'task-turn',
      turnId: 'turn-1',
    });
    expect(result.current.activeSession?.messages.filter(message => message.text === '分析品牌'))
      .toHaveLength(1);
  });

  it('refreshes the session after a task request error and keeps a persisted message for the turn', async () => {
    const persistedAfterError: Session = {
      ...restoredSession,
      messages: [
        ...restoredSession.messages,
        {
          id: 'message-error',
          sender: 'user',
          text: '会失败但已落库',
          timestamp: '10:00',
          turnId: 'turn-1',
        },
      ],
    };
    vi.mocked(getSession)
      .mockResolvedValueOnce(restoredSession)
      .mockResolvedValueOnce(persistedAfterError);
    vi.mocked(createTask).mockRejectedValue(new Error('TASK_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await expect(result.current.appendMessage('会失败但已落库')).rejects.toThrow('TASK_FAILED');
    });

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(result.current.activeSession?.messages.at(-1)).toMatchObject({
      id: 'message-error',
      turnId: 'turn-1',
    });
  });

  it('subscribes to a task that persisted before the create response failed', async () => {
    const persistedAfterError: Session = {
      ...restoredSession,
      status: 'analyzing',
      analysis: {
        taskId: 'task-persisted',
        status: 'pending',
        kind: 'agent',
      },
      messages: [
        ...restoredSession.messages,
        {
          id: 'message-persisted',
          sender: 'user',
          text: '任务已落库',
          timestamp: '10:00',
          turnId: 'turn-1',
          taskId: 'task-persisted',
        },
      ],
    };
    vi.mocked(getSession)
      .mockResolvedValueOnce(restoredSession)
      .mockResolvedValueOnce(persistedAfterError);
    vi.mocked(createTask).mockRejectedValue(new Error('NETWORK_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));

    await act(async () => {
      await expect(result.current.appendMessage('任务已落库')).rejects.toThrow('NETWORK_FAILED');
    });

    expect(result.current.activeTaskId).toBe('task-persisted');
    expect(result.current.isAnalyzing).toBe(true);
  });

  it('does not create a second task while the active task is still running', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1',
      session_id: 'session-1',
      status: 'pending',
      estimated_points: 0,
      error_code: null,
      latest_report_id: null,
    }));
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
    const pendingTask = deferred<TaskCreateResult>();
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
      pendingTask.resolve(taskOutcome({
        id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0, error_code: null, latest_report_id: null,
      }));
      await firstSubmission;
    });
  });

  it('loads artifacts summary when a session activates and refreshes on artifact updates', async () => {
    const summary: ApiArtifactsSummary = {
      brand: { latest_artifact: null, unread: false },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: {
        latest_artifact: {
          artifact_id: 'artifact-1', artifact_type: 'kol_report', title: 'KOL 分析',
          version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: true,
      },
      kol_selection: { latest_artifact: null, unread: false },
    };
    vi.mocked(getArtifactsSummary).mockResolvedValue(summary);
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      analysis: { taskId: 'task-1', status: 'running' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected', status: 'running',
    });
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.artifactsSummary).toEqual(summary));
    expect(getArtifactsSummary).toHaveBeenCalledWith('session-1');
    const callsAfterActivate = vi.mocked(getArtifactsSummary).mock.calls.length;

    // artifact.updated 到达（artifactUpdates 记账更新）→ 重新拉取 summary。
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 2, assistantDraft: '', connection: 'connected', status: 'running',
      artifactUpdates: {
        brand: { artifactId: 'artifact-2', moduleKey: 'brand', version: 1, title: '品牌分析' },
      },
    });
    rerender();
    await waitFor(() => expect(vi.mocked(getArtifactsSummary).mock.calls.length).toBeGreaterThan(callsAfterActivate));
  });

  it('markArtifactSeen calls the api and clears the unread flag locally', async () => {
    const summary: ApiArtifactsSummary = {
      brand: { latest_artifact: null, unread: false },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: {
        latest_artifact: {
          artifact_id: 'artifact-1', artifact_type: 'kol_report', title: 'KOL 分析',
          version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: true,
      },
      kol_selection: { latest_artifact: null, unread: false },
    };
    vi.mocked(getArtifactsSummary).mockResolvedValue(summary);
    vi.mocked(markArtifactRead).mockResolvedValue(undefined);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.artifactsSummary?.kol_analysis.unread).toBe(true));

    await act(async () => {
      await result.current.markArtifactSeen('kol_analysis', 'artifact-1');
    });

    expect(markArtifactRead).toHaveBeenCalledWith('session-1', 'kol_analysis', 'artifact-1');
    expect(result.current.activeSession?.artifactsSummary?.kol_analysis.unread).toBe(false);
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

  it('retries a terminal message in the same session and clears old artifacts', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      messages: restoredSession.messages.map(message => ({ ...message, taskId: 'task-old' })),
      analysis: { taskId: 'task-old', status: 'failed', analysisReportId: 'analysis-report-old' },
      analysisReport: {
        id: 'analysis-report-old', task_id: 'task-old', version: 1, title: '旧报告',
        blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-15T10:00:00Z',
      },
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
    expect(result.current.activeSession?.analysis?.analysisReportId).toBeUndefined();
    expect(result.current.activeSession?.analysisReport).toBeUndefined();
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

  it('fetches a session-level analysis report (task_id null) announced by report.updated', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running', kind: 'agent' },
    });
    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 1, assistantDraft: '', connection: 'connected',
      status: 'running', visibleAnalysisReportId: 'analysis-report-kol',
    });
    vi.mocked(getAnalysisReport).mockResolvedValue({
      id: 'analysis-report-kol', task_id: null, version: 1, title: 'KOL 圈选分析',
      blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-22T10:00:00Z',
    });
    const { result } = renderHook(() => useWorkspace('user-a'));

    await waitFor(() => expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-kol'));
    expect(getAnalysisReport).toHaveBeenCalledWith('analysis-report-kol');
    expect(result.current.activeSession?.analysis?.analysisReportId).toBe('analysis-report-kol');
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

  it('keeps a session-level analysis report (task_id null) when the task runtime updates', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running', kind: 'agent' },
    });
    const runtime = (lastEventId: number) => ({
      taskId: 'task-1', lastEventId, assistantDraft: '', connection: 'connected' as const, status: 'running' as const,
    });
    vi.mocked(useTaskStream).mockReturnValue(runtime(1));
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-1'));

    act(() => {
      result.current.setAnalysisReport('session-1', {
        id: 'analysis-report-kol', task_id: null, version: 1, title: 'KOL 匹配度分析',
        blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-21T10:00:00Z',
      });
    });
    expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-kol');

    // 下一条 SSE 事件触发 runtime effect：会话级报告不随任务失效。
    vi.mocked(useTaskStream).mockReturnValue(runtime(2));
    rerender();

    expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-kol');
  });

  it('clears a task-level report belonging to another task when the task runtime updates', async () => {
    vi.mocked(getSession).mockResolvedValue({
      ...restoredSession,
      status: 'analyzing',
      analysis: { taskId: 'task-1', status: 'running', kind: 'agent' },
    });
    const runtime = (lastEventId: number) => ({
      taskId: 'task-1', lastEventId, assistantDraft: '', connection: 'connected' as const, status: 'running' as const,
    });
    vi.mocked(useTaskStream).mockReturnValue(runtime(1));
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeTaskId).toBe('task-1'));

    act(() => {
      result.current.setAnalysisReport('session-1', {
        id: 'analysis-report-stale', task_id: 'task-old', version: 1, title: '旧任务报告',
        blocks: [], conclusion: null, status: 'completed', generated_at: '2026-07-15T10:00:00Z',
      });
    });
    expect(result.current.activeSession?.analysisReport?.id).toBe('analysis-report-stale');

    vi.mocked(useTaskStream).mockReturnValue(runtime(2));
    rerender();

    expect(result.current.activeSession?.analysisReport).toBeUndefined();
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
    await waitFor(() => expect(result.current.activeSession?.messages).toHaveLength(3));
    rerender();
    expect(result.current.activeSession?.messages.filter(message => message.id === 'error-1')).toHaveLength(1);
  });

  it('cancelActiveTask calls cancelTask for the active task and sets cancelRequested', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));
    await act(async () => {
      await result.current.appendMessage('先跑一个任务');
    });
    expect(result.current.activeTaskId).toBe('task-1');
    expect(result.current.isAnalyzing).toBe(true);
    expect(result.current.cancelRequested).toBe(false);

    await act(async () => {
      await result.current.cancelActiveTask();
    });

    expect(cancelTask).toHaveBeenCalledWith('task-1');
    expect(result.current.cancelRequested).toBe(true);
  });

  it('keeps cancelRequested latched after the cancel API resolves until the terminal event arrives', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
    const pendingCancel = deferred<Awaited<ReturnType<typeof cancelTask>>>();
    vi.mocked(cancelTask).mockReturnValue(pendingCancel.promise);
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));
    await act(async () => {
      await result.current.appendMessage('先跑一个任务');
    });

    let cancelPromise!: Promise<unknown>;
    act(() => {
      cancelPromise = result.current.cancelActiveTask();
    });
    await waitFor(() => expect(result.current.cancelRequested).toBe(true));
    expect(cancelTask).toHaveBeenCalledWith('task-1');

    await act(async () => {
      pendingCancel.resolve({
        id: 'task-1', session_id: 'session-1', status: 'running', estimated_points: 0,
        error_code: null, latest_report_id: null,
      });
      await cancelPromise;
    });

    // API 已 resolve，但 task.cancelled（终态 SSE）未到：latch 保持，重复点击不再调用。
    expect(result.current.cancelRequested).toBe(true);
    await act(async () => {
      await result.current.cancelActiveTask();
    });
    expect(cancelTask).toHaveBeenCalledTimes(1);
  });

  it('resets cancelRequested once the task runtime reaches a terminal status', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
    const { result, rerender } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));
    await act(async () => {
      await result.current.appendMessage('先跑一个任务');
    });
    await act(async () => {
      await result.current.cancelActiveTask();
    });
    expect(result.current.cancelRequested).toBe(true);

    vi.mocked(useTaskStream).mockReturnValue({
      taskId: 'task-1', lastEventId: 3, assistantDraft: '', connection: 'closed', status: 'cancelled',
    });
    rerender();

    await waitFor(() => expect(result.current.cancelRequested).toBe(false));
    expect(result.current.isAnalyzing).toBe(false);
  });

  it('cancelActiveTask is a no-op when there is no active task', async () => {
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));
    expect(result.current.activeTaskId).toBeUndefined();

    await act(async () => {
      await result.current.cancelActiveTask();
    });

    expect(cancelTask).not.toHaveBeenCalled();
    expect(result.current.cancelRequested).toBe(false);
  });

  it('resets cancelRequested immediately when the cancel request fails so it can be retried', async () => {
    vi.mocked(createTask).mockResolvedValue(taskOutcome({
      id: 'task-1', session_id: 'session-1', status: 'pending', estimated_points: 0,
      error_code: null, latest_report_id: null,
    }));
    vi.mocked(cancelTask).mockRejectedValue(new Error('CANCEL_FAILED'));
    const { result } = renderHook(() => useWorkspace('user-a'));
    await waitFor(() => expect(result.current.activeSession?.id).toBe('session-1'));
    await act(async () => {
      await result.current.appendMessage('先跑一个任务');
    });

    await act(async () => {
      await result.current.cancelActiveTask();
    });

    expect(cancelTask).toHaveBeenCalledWith('task-1');
    expect(result.current.cancelRequested).toBe(false);

    // 失败复位后可重试。
    vi.mocked(cancelTask).mockResolvedValue({
      id: 'task-1', session_id: 'session-1', status: 'running', estimated_points: 0,
      error_code: null, latest_report_id: null,
    });
    await act(async () => {
      await result.current.cancelActiveTask();
    });
    expect(cancelTask).toHaveBeenCalledTimes(2);
    expect(result.current.cancelRequested).toBe(true);
  });
});
