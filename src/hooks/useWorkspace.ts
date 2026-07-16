import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  createSession as createSessionRequest,
  deleteSession as deleteSessionRequest,
  getSession,
  listSessions,
  updateSession as updateSessionRequest,
} from '../api/sessions';
import { createTask, getCandidates, getReport, retryTask } from '../api/tasks';
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
    const [candidateResponse, reportResponse] = await Promise.all([
      analysis.candidateVersion === undefined ? Promise.resolve(undefined) : getCandidates(analysis.taskId),
      analysis.reportId === undefined ? Promise.resolve(undefined) : getReport(analysis.reportId),
    ]);
    if (
      generationRef.current !== generation
      || !sessionOperationIsCurrent(session.id, operationEpoch)
    ) return session;
    const candidatePage = candidateResponse?.task_id === analysis.taskId
      && candidateResponse.version === analysis.candidateVersion
      ? candidateResponse
      : undefined;
    const candidateResponseIsValid = candidateResponse === undefined || candidatePage !== undefined;
    const matchingReport = candidateResponseIsValid
      && reportResponse?.task_id === analysis.taskId
      && reportResponse.candidate_version === analysis.candidateVersion
      ? reportResponse
      : undefined;
    return {
      ...session,
      analysis: {
        ...analysis,
        candidateVersion: analysis.candidateVersion,
        reportId: matchingReport?.id,
      },
      candidates: candidatePage?.items.map(candidate => ({
        id: candidate.id,
        kolId: candidate.kol_id,
        platform: candidate.platform,
        platformAccountId: candidate.platform_account_id,
        nickname: candidate.nickname ?? undefined,
        profileUrl: candidate.profile_url ?? undefined,
        rank: candidate.rank,
        totalScore: candidate.total_score,
        scores: candidate.scores,
        matchedConditions: candidate.matched_conditions,
        risks: candidate.risks,
        recommendation: candidate.recommendation,
        metrics: candidate.metrics,
      })),
      biReport: matchingReport,
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
        analysis: { taskId: task.id, status: task.status },
        candidates: undefined,
        biReport: undefined,
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
        analysis: { taskId: task.id, status: task.status },
        candidates: undefined,
        biReport: undefined,
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
        candidateVersion: currentTaskRuntime.candidateVersion ?? session.analysis.candidateVersion,
        reportId: currentTaskRuntime.visibleReportId ?? (
          currentTaskRuntime.candidateVersion !== undefined
            && currentTaskRuntime.candidateVersion !== session.analysis.candidateVersion
            ? undefined
            : session.analysis.reportId
        ),
      },
      biReport: currentTaskRuntime.candidateVersion === undefined
        || session.biReport?.candidate_version !== currentTaskRuntime.candidateVersion
        ? undefined
        : session.biReport,
      candidates: currentTaskRuntime.candidateVersion === undefined
        || session.analysis.candidateVersion !== currentTaskRuntime.candidateVersion
        ? undefined
        : session.candidates,
    } : session));
    if (currentTaskRuntime.candidateVersion !== undefined) {
      const requestedTaskId = activeTaskId;
      const requestedCandidateVersion = currentTaskRuntime.candidateVersion;
      void getCandidates(requestedTaskId)
        .then(page => {
          if (
            generationRef.current !== generation
            || page.task_id !== requestedTaskId
            || page.version !== requestedCandidateVersion
          ) return;
          setSessions(current => current.map(session => session.analysis?.taskId === requestedTaskId
            && session.analysis.candidateVersion === requestedCandidateVersion ? {
            ...session,
            candidates: page.items.map(candidate => ({
              id: candidate.id,
              kolId: candidate.kol_id,
              platform: candidate.platform,
              platformAccountId: candidate.platform_account_id,
              nickname: candidate.nickname ?? undefined,
              profileUrl: candidate.profile_url ?? undefined,
              rank: candidate.rank,
              totalScore: candidate.total_score,
              scores: candidate.scores,
              matchedConditions: candidate.matched_conditions,
              risks: candidate.risks,
              recommendation: candidate.recommendation,
              metrics: candidate.metrics,
            })),
          } : session));
        })
        .catch(() => undefined);
    }
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
    if (currentTaskRuntime.visibleReportId) {
      const requestedTaskId = activeTaskId;
      const requestedCandidateVersion = currentTaskRuntime.candidateVersion;
      const requestedReportId = currentTaskRuntime.visibleReportId;
      void getReport(requestedReportId)
        .then(report => {
          if (
            generationRef.current !== generation
            || report.task_id !== requestedTaskId
            || report.candidate_version !== requestedCandidateVersion
          ) return;
          setSessions(current => current.map(session => session.analysis?.taskId === requestedTaskId
            && session.analysis.candidateVersion === requestedCandidateVersion
            && session.analysis.reportId === requestedReportId
            && report.candidate_version === requestedCandidateVersion ? {
            ...session,
            biReport: report,
          } : session));
        })
        .catch(() => undefined);
    }
  }, [activeTaskId, currentTaskRuntime]);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );
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
    error,
    reload,
    selectSession,
    createSession,
    updateSession,
    deleteSession,
    appendMessage,
    retryMessage,
  };
}
