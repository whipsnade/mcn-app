import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  createSession as createSessionRequest,
  deleteSession as deleteSessionRequest,
  getSession,
  listSessions,
  toMessage,
  updateSession as updateSessionRequest,
} from '../api/sessions';
import { isBrainstormProfileReady, postBrainstorm } from '../api/brainstorm';
import { createTask, getAnalysisReport, getTask, retryFollowups as retryFollowupsRequest, retryTask } from '../api/tasks';
import type { CreateSessionInput } from '../api/contracts';
import { useTaskStream } from './useTaskStream';
import { isTerminalTaskStatus } from '../state/taskEvents';
import type { Message, Session } from '../types';


function replaceSession(sessions: Session[], nextSession: Session): Session[] {
  const exists = sessions.some(session => session.id === nextSession.id);
  if (!exists) {
    return [nextSession, ...sessions];
  }
  return sessions.map(session => session.id === nextSession.id ? nextSession : session);
}

function taskIsInProgress(status: string | undefined): boolean {
  return !isTerminalTaskStatus(status);
}

interface TaskCreateLock {
  sessionId: string;
  token: symbol;
}


export function useWorkspace(userId?: string) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [activeTaskId, setActiveTaskId] = useState<string>();
  const [isClarifying, setIsClarifying] = useState(false);
  const generationRef = useRef(0);
  const selectionRequestRef = useRef(0);
  const sessionsRef = useRef<Session[]>([]);
  const activeSessionIdRef = useRef<string>();
  const deletedSessionIdsRef = useRef(new Set<string>());
  const sessionOperationEpochsRef = useRef(new Map<string, number>());
  const taskCreateInFlightRef = useRef<TaskCreateLock | null>(null);
  const taskRuntime = useTaskStream(activeTaskId);
  const currentTaskRuntime = taskRuntime?.taskId === activeTaskId ? taskRuntime : undefined;

  const getSessionOperationEpoch = useCallback(
    (id: string) => sessionOperationEpochsRef.current.get(id) ?? 0,
    [],
  );
  const sessionOperationIsCurrent = useCallback((id: string, epoch: number) => (
    !deletedSessionIdsRef.current.has(id)
    && (sessionOperationEpochsRef.current.get(id) ?? 0) === epoch
  ), []);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  const hydrateAnalysis = useCallback(async (
    session: Session,
    generation: number,
    operationEpoch = getSessionOperationEpoch(session.id),
  ): Promise<Session> => {
    const analysis = session.analysis;
    if (
      !analysis
      || generationRef.current !== generation
      || !sessionOperationIsCurrent(session.id, operationEpoch)
    ) return session;
    const analysisReportResponse = analysis.analysisReportId === undefined
      ? undefined
      : await getAnalysisReport(analysis.analysisReportId);
    if (
      generationRef.current !== generation
      || !sessionOperationIsCurrent(session.id, operationEpoch)
    ) return session;
    const matchingAnalysisReport = analysisReportResponse?.task_id === analysis.taskId
      ? analysisReportResponse
      : undefined;
    return {
      ...session,
      analysis: {
        ...analysis,
        analysisReportId: matchingAnalysisReport?.id,
      },
      analysisReport: matchingAnalysisReport,
    };
  }, [getSessionOperationEpoch, sessionOperationIsCurrent]);

  const load = useCallback(async (generation: number) => {
    setLoading(true);
    setError(undefined);
    try {
      const loaded = (await listSessions()).filter(session => !deletedSessionIdsRef.current.has(session.id));
      const firstSummary = loaded[0];
      const firstEpoch = firstSummary ? getSessionOperationEpoch(firstSummary.id) : undefined;
      const rawFirst = firstSummary ? await getSession(firstSummary.id) : undefined;
      const hydratedFirst = rawFirst && firstEpoch !== undefined
        ? await hydrateAnalysis(rawFirst, generation, firstEpoch)
        : undefined;
      if (generationRef.current !== generation) return;
      const availableSessions = loaded.filter(session => !deletedSessionIdsRef.current.has(session.id));
      const first = hydratedFirst && firstEpoch !== undefined
        && sessionOperationIsCurrent(hydratedFirst.id, firstEpoch)
        ? hydratedFirst
        : undefined;
      const nextSessions = first ? replaceSession(availableSessions, first) : availableSessions;
      const nextActiveSession = first ?? nextSessions[0];
      sessionsRef.current = nextSessions;
      activeSessionIdRef.current = nextActiveSession?.id;
      setSessions(nextSessions);
      setActiveSessionId(nextActiveSession?.id);
      setActiveTaskId(nextActiveSession?.analysis?.taskId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '加载会话失败');
      }
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, [getSessionOperationEpoch, hydrateAnalysis, sessionOperationIsCurrent]);

  useEffect(() => {
    const generation = ++generationRef.current;
    selectionRequestRef.current += 1;
    sessionsRef.current = [];
    activeSessionIdRef.current = undefined;
    deletedSessionIdsRef.current.clear();
    sessionOperationEpochsRef.current.clear();
    setSessions([]);
    setActiveSessionId(undefined);
    setError(undefined);
    setLoading(false);
    setBusy(false);
    setActiveTaskId(undefined);
    taskCreateInFlightRef.current = null;
    if (userId) void load(generation);
    return () => {
      if (generationRef.current === generation) generationRef.current += 1;
    };
  }, [load, userId]);

  const reload = useCallback(async () => {
    if (!userId) return;
    await load(generationRef.current);
  }, [load, userId]);

  const selectSession = useCallback(async (id: string) => {
    if (!userId) return;
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(id);
    if (!sessionOperationIsCurrent(id, operationEpoch)) return;
    const selectionRequest = ++selectionRequestRef.current;
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
    setActiveTaskId(undefined);
    setError(undefined);
    try {
      const loaded = await hydrateAnalysis(await getSession(id), generation, operationEpoch);
      if (
        generationRef.current === generation
        && selectionRequestRef.current === selectionRequest
        && activeSessionIdRef.current === id
        && sessionOperationIsCurrent(id, operationEpoch)
      ) {
        setSessions(current => {
          const nextSessions = replaceSession(current, loaded);
          sessionsRef.current = nextSessions;
          return nextSessions;
        });
        setActiveTaskId(loaded.analysis?.taskId);
      }
    } catch (reason) {
      if (
        generationRef.current === generation
        && selectionRequestRef.current === selectionRequest
        && activeSessionIdRef.current === id
        && sessionOperationIsCurrent(id, operationEpoch)
      ) {
        setError(reason instanceof Error ? reason.message : '恢复会话失败');
      }
    }
  }, [getSessionOperationEpoch, hydrateAnalysis, sessionOperationIsCurrent, userId]);

  const createSession = useCallback(async (input: CreateSessionInput) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const created = await createSessionRequest(input);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      setSessions(current => {
        const nextSessions = replaceSession(current, created);
        sessionsRef.current = nextSessions;
        return nextSessions;
      });
      selectionRequestRef.current += 1;
      activeSessionIdRef.current = created.id;
      setActiveSessionId(created.id);
      setActiveTaskId(created.analysis?.taskId);
      return created;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '创建会话失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [userId]);

  const updateSession = useCallback(async (id: string, changes: Record<string, unknown>) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(id);
    setBusy(true);
    setError(undefined);
    try {
      const updated = await updateSessionRequest(id, changes);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      if (!sessionOperationIsCurrent(id, operationEpoch)) return updated;
      setSessions(current => {
        const nextSessions = replaceSession(current, updated);
        sessionsRef.current = nextSessions;
        return nextSessions;
      });
      return updated;
    } catch (reason) {
      if (
        generationRef.current === generation
        && sessionOperationIsCurrent(id, operationEpoch)
      ) {
        setError(reason instanceof Error ? reason.message : '更新会话失败');
      }
      throw reason;
    } finally {
      if (
        generationRef.current === generation
        && sessionOperationIsCurrent(id, operationEpoch)
      ) setBusy(false);
    }
  }, [getSessionOperationEpoch, sessionOperationIsCurrent, userId]);

  const deleteSession = useCallback(async (id: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      await deleteSessionRequest(id);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');

      deletedSessionIdsRef.current.add(id);
      sessionOperationEpochsRef.current.set(id, getSessionOperationEpoch(id) + 1);

      const remainingSessions = sessionsRef.current.filter(session => session.id !== id);
      sessionsRef.current = remainingSessions;
      setSessions(remainingSessions);

      if (taskCreateInFlightRef.current?.sessionId === id) {
        taskCreateInFlightRef.current = null;
      }

      if (activeSessionIdRef.current !== id) return;

      selectionRequestRef.current += 1;
      setActiveTaskId(undefined);
      const nextSession = remainingSessions[0];
      if (!nextSession) {
        activeSessionIdRef.current = undefined;
        setActiveSessionId(undefined);
        return;
      }

      await selectSession(nextSession.id);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '删除会话失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [getSessionOperationEpoch, selectSession, userId]);

  const appendMessage = useCallback(async (content: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    if (!activeSessionId) return;
    if (taskCreateInFlightRef.current) throw new Error('TASK_IN_PROGRESS');
    const activeSession = sessions.find(session => session.id === activeSessionId);
    if (activeSession?.analysis && taskIsInProgress(activeSession.analysis.status)) {
      throw new Error('TASK_IN_PROGRESS');
    }
    const generation = generationRef.current;
    const requestedSessionId = activeSessionId;
    const operationEpoch = getSessionOperationEpoch(requestedSessionId);
    const taskCreateLock: TaskCreateLock = {
      sessionId: requestedSessionId,
      token: Symbol(requestedSessionId),
    };
    taskCreateInFlightRef.current = taskCreateLock;
    setBusy(true);
    setError(undefined);
    try {
      if (activeSession && !isBrainstormProfileReady(activeSession)) {
        // 画像未 ready：走 brainstorm 澄清，同步请求-响应，ready 后直接绑定已创建的任务。
        const optimisticMessage: Message = {
          id: `pending-brainstorm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
          sender: 'user',
          text: content,
          timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        };
        setIsClarifying(true);
        setSessions(current => current.map(session => session.id === requestedSessionId ? {
          ...session,
          messages: [...session.messages, optimisticMessage],
        } : session));
        let response;
        try {
          response = await postBrainstorm(requestedSessionId, content);
        } catch (reason) {
          // 失败时回滚乐观消息，输入框草稿由 ChatArea 保留（与 createTask 失败处理一致）。
          if (
            generationRef.current === generation
            && sessionOperationIsCurrent(requestedSessionId, operationEpoch)
          ) {
            setSessions(current => current.map(session => session.id === requestedSessionId ? {
              ...session,
              messages: session.messages.filter(message => message.id !== optimisticMessage.id),
            } : session));
          }
          throw reason;
        } finally {
          setIsClarifying(false);
        }
        if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
        if (!sessionOperationIsCurrent(requestedSessionId, operationEpoch)) return response;
        const taskId = response.ready ? response.task_id : null;
        const assistantMessage = toMessage(response.message);
        setSessions(current => current.map(session => session.id === requestedSessionId ? {
          ...session,
          status: taskId ? 'analyzing' : session.status,
          messages: [
            ...session.messages.map(message => message.id === optimisticMessage.id && taskId
              ? { ...message, taskId }
              : message),
            assistantMessage,
          ],
          analysis: taskId
            ? { taskId, status: 'pending', kind: 'agent' as const }
            : session.analysis,
          analysisReport: taskId ? undefined : session.analysisReport,
        } : session));
        if (taskId && activeSessionIdRef.current === requestedSessionId) {
          setActiveTaskId(taskId);
        }
        return response;
      }
      const task = await createTask(requestedSessionId, { content });
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      if (!sessionOperationIsCurrent(requestedSessionId, operationEpoch)) return task;
      const pendingMessage: Message = {
        id: task.trigger_message_id ?? `pending-${task.id}`,
        sender: 'user',
        text: content,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        taskId: task.id,
      };
      setSessions(current => current.map(session => session.id === requestedSessionId ? {
        ...session,
        status: 'analyzing',
        messages: [...session.messages, pendingMessage],
        analysis: { taskId: task.id, status: task.status, kind: task.kind },
        analysisReport: undefined,
      } : session));
      if (activeSessionIdRef.current === requestedSessionId) {
        setActiveTaskId(task.id);
      }
      return task;
    } catch (reason) {
      if (
        generationRef.current === generation
        && sessionOperationIsCurrent(requestedSessionId, operationEpoch)
      ) {
        setError(reason instanceof Error ? reason.message : '保存消息失败');
      }
      throw reason;
    } finally {
      if (taskCreateInFlightRef.current?.token === taskCreateLock.token) {
        taskCreateInFlightRef.current = null;
        if (generationRef.current === generation) setBusy(false);
      }
    }
  }, [activeSessionId, getSessionOperationEpoch, sessionOperationIsCurrent, sessions, userId]);

  const retryMessage = useCallback(async (messageId: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    if (taskCreateInFlightRef.current) throw new Error('TASK_IN_PROGRESS');
    const activeSession = sessions.find(session => session.id === activeSessionId);
    const message = activeSession?.messages.find(item => item.id === messageId);
    if (!activeSession || !message?.taskId) throw new Error('RETRY_TASK_NOT_FOUND');
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(activeSession.id);
    const taskCreateLock: TaskCreateLock = {
      sessionId: activeSession.id,
      token: Symbol(activeSession.id),
    };
    taskCreateInFlightRef.current = taskCreateLock;
    setBusy(true);
    setError(undefined);
    try {
      const task = await retryTask(message.taskId);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      if (!sessionOperationIsCurrent(activeSession.id, operationEpoch)) return task;
      setSessions(current => current.map(session => session.id === activeSession.id ? {
        ...session,
        status: 'analyzing',
        messages: session.messages.map(item => item.id === messageId ? { ...item, taskId: task.id } : item),
        analysis: { taskId: task.id, status: task.status, kind: task.kind },
        analysisReport: undefined,
      } : session));
      if (activeSessionIdRef.current === activeSession.id) {
        setActiveTaskId(task.id);
      }
      return task;
    } catch (reason) {
      if (
        generationRef.current === generation
        && sessionOperationIsCurrent(activeSession.id, operationEpoch)
      ) {
        setError(reason instanceof Error ? reason.message : '再次执行失败');
      }
      throw reason;
    } finally {
      if (taskCreateInFlightRef.current?.token === taskCreateLock.token) {
        taskCreateInFlightRef.current = null;
        if (generationRef.current === generation) setBusy(false);
      }
    }
  }, [activeSessionId, getSessionOperationEpoch, sessionOperationIsCurrent, sessions, userId]);

  const retryFollowups = useCallback(async () => {
    if (!userId || !activeSessionId) throw new Error('AUTH_EXPIRED');
    const session = sessions.find(item => item.id === activeSessionId);
    const taskId = session?.analysis?.taskId;
    if (!taskId || session.analysis?.followupStatus !== 'failed') throw new Error('FOLLOWUP_RETRY_NOT_AVAILABLE');
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(activeSessionId);
    setBusy(true);
    setError(undefined);
    try {
      const task = await retryFollowupsRequest(taskId);
      if (
        generationRef.current !== generation
        || !sessionOperationIsCurrent(activeSessionId, operationEpoch)
      ) return task;
      setSessions(current => current.map(item => item.id === activeSessionId && item.analysis?.taskId === taskId ? {
        ...item,
        analysis: {
          ...item.analysis,
          // The retry endpoint is asynchronous; a 202 must enter pending
          // even if its response was read from a stale failed snapshot.
          followupStatus: 'pending',
          followupSuggestions: [],
          followupError: undefined,
        },
      } : item));
      return task;
    } catch (reason) {
      if (generationRef.current === generation && sessionOperationIsCurrent(activeSessionId, operationEpoch)) {
        setError(reason instanceof Error ? reason.message : '重试建议生成失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [activeSessionId, getSessionOperationEpoch, sessionOperationIsCurrent, sessions, userId]);

  useEffect(() => {
    if (!currentTaskRuntime || !activeTaskId) return;
    const generation = generationRef.current;
    setSessions(current => current.map(session => session.analysis?.taskId === activeTaskId ? {
      ...session,
      status: currentTaskRuntime.status === 'completed' || currentTaskRuntime.status === 'completed_with_warnings'
        ? 'completed'
        : session.status,
      analysis: {
        ...session.analysis,
        status: currentTaskRuntime.status ?? session.analysis.status,
        analysisReportId: currentTaskRuntime.visibleAnalysisReportId ?? session.analysis.analysisReportId,
        followupStatus: currentTaskRuntime.followupStatus ?? session.analysis.followupStatus,
        followupSuggestions: currentTaskRuntime.followupSuggestions ?? session.analysis.followupSuggestions,
        followupError: currentTaskRuntime.followupError
          ? { message: currentTaskRuntime.followupError }
          : currentTaskRuntime.followupStatus === 'completed'
            ? undefined
            : session.analysis.followupError,
      },
      // 新任务开始后清空属于旧任务的分析报告，等待 report.updated 事件回填。
      analysisReport: session.analysisReport?.task_id === activeTaskId ? session.analysisReport : undefined,
    } : session));
    if (currentTaskRuntime.errorMessage && currentTaskRuntime.errorMessageId) {
      const errorMessageId = currentTaskRuntime.errorMessageId;
      setSessions(current => current.map(session => {
        if (session.analysis?.taskId !== activeTaskId || session.messages.some(message => message.id === errorMessageId)) {
          return session;
        }
        return {
          ...session,
          messages: [...session.messages, {
            id: errorMessageId,
            sender: 'ai' as const,
            text: currentTaskRuntime.errorMessage ?? '分析任务执行失败，请稍后重试。',
            timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
          }],
        };
      }));
    }
    if (currentTaskRuntime.visibleAnalysisReportId) {
      const requestedTaskId = activeTaskId;
      const requestedReportId = currentTaskRuntime.visibleAnalysisReportId;
      void getAnalysisReport(requestedReportId)
        .then(report => {
          if (
            generationRef.current !== generation
            || report.task_id !== requestedTaskId
          ) return;
          setSessions(current => current.map(session => session.analysis?.taskId === requestedTaskId
            && session.analysis.analysisReportId === requestedReportId ? {
            ...session,
            analysisReport: report,
          } : session));
        })
        .catch(() => undefined);
    }
  }, [activeTaskId, currentTaskRuntime]);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );

  useEffect(() => {
    const taskId = activeTaskId;
    const sessionId = activeSessionId;
    if (!taskId || !sessionId || activeSession?.analysis?.followupStatus !== 'pending') return;
    let stopped = false;
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(sessionId);
    const poll = async () => {
      try {
        const task = await getTask(taskId);
        if (
          stopped
          || generationRef.current !== generation
          || !sessionOperationIsCurrent(sessionId, operationEpoch)
          || task.id !== taskId
        ) return;
        if (task.followup_suggestions_status) {
          setSessions(current => current.map(item => item.id === sessionId && item.analysis?.taskId === taskId ? {
            ...item,
            analysis: {
              ...item.analysis,
              followupStatus: task.followup_suggestions_status ?? undefined,
              followupSuggestions: task.followup_suggestions ?? [],
              followupError: task.followup_error ?? undefined,
            },
          } : item));
        }
      } catch {
        // SSE remains the primary path; transient polling failures are retried.
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [activeSession?.analysis?.followupStatus, activeSessionId, activeTaskId, getSessionOperationEpoch, sessionOperationIsCurrent]);
  const isAnalyzing = busy || Boolean(
    activeSession?.analysis && taskIsInProgress(currentTaskRuntime?.status ?? activeSession.analysis.status),
  );

  return {
    sessions,
    activeSession,
    activeSessionId,
    activeTaskId,
    taskRuntime: currentTaskRuntime,
    loading,
    busy,
    isAnalyzing,
    isClarifying,
    error,
    reload,
    selectSession,
    createSession,
    updateSession,
    deleteSession,
    appendMessage,
    retryMessage,
    retryFollowups,
  };
}
