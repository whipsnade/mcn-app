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
  const taskCreateInFlightRef = useRef(false);
  const taskRuntime = useTaskStream(activeTaskId);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  const hydrateAnalysis = useCallback(async (session: Session, generation: number): Promise<Session> => {
    const analysis = session.analysis;
    if (!analysis || generationRef.current !== generation) return session;
    const [candidatePage, report] = await Promise.all([
      analysis.candidateVersion === undefined ? Promise.resolve(undefined) : getCandidates(analysis.taskId),
      analysis.reportId === undefined ? Promise.resolve(undefined) : getReport(analysis.reportId),
    ]);
    if (generationRef.current !== generation) return session;
    const resolvedCandidateVersion = candidatePage?.version ?? analysis.candidateVersion;
    const matchingReport = report?.candidate_version === resolvedCandidateVersion ? report : undefined;
    return {
      ...session,
      analysis: {
        ...analysis,
        candidateVersion: resolvedCandidateVersion,
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
  }, []);

  const load = useCallback(async (generation: number) => {
    setLoading(true);
    setError(undefined);
    try {
      const loaded = await listSessions();
      const rawFirst = loaded[0] ? await getSession(loaded[0].id) : undefined;
      const first = rawFirst ? await hydrateAnalysis(rawFirst, generation) : undefined;
      if (generationRef.current !== generation) return;
      const nextSessions = first ? replaceSession(loaded, first) : loaded;
      sessionsRef.current = nextSessions;
      activeSessionIdRef.current = first?.id;
      setSessions(nextSessions);
      setActiveSessionId(first?.id);
      setActiveTaskId(first?.analysis?.taskId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '加载会话失败');
      }
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, [hydrateAnalysis]);

  useEffect(() => {
    const generation = ++generationRef.current;
    selectionRequestRef.current += 1;
    sessionsRef.current = [];
    activeSessionIdRef.current = undefined;
    setSessions([]);
    setActiveSessionId(undefined);
    setError(undefined);
    setLoading(false);
    setBusy(false);
    setActiveTaskId(undefined);
    taskCreateInFlightRef.current = false;
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
    const selectionRequest = ++selectionRequestRef.current;
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
    setActiveTaskId(undefined);
    setError(undefined);
    try {
      const loaded = await hydrateAnalysis(await getSession(id), generation);
      if (
        generationRef.current === generation
        && selectionRequestRef.current === selectionRequest
        && activeSessionIdRef.current === id
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
      ) {
        setError(reason instanceof Error ? reason.message : '恢复会话失败');
      }
    }
  }, [hydrateAnalysis, userId]);

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
    setBusy(true);
    setError(undefined);
    try {
      const updated = await updateSessionRequest(id, changes);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      setSessions(current => {
        const nextSessions = replaceSession(current, updated);
        sessionsRef.current = nextSessions;
        return nextSessions;
      });
      return updated;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '更新会话失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [userId]);

  const deleteSession = useCallback(async (id: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      await deleteSessionRequest(id);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');

      const remainingSessions = sessionsRef.current.filter(session => session.id !== id);
      sessionsRef.current = remainingSessions;
      setSessions(remainingSessions);

      if (activeSessionIdRef.current !== id) return;

      selectionRequestRef.current += 1;
      taskCreateInFlightRef.current = false;
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
  }, [selectSession, userId]);

  const appendMessage = useCallback(async (content: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    if (!activeSessionId) return;
    if (taskCreateInFlightRef.current) throw new Error('TASK_IN_PROGRESS');
    const activeSession = sessions.find(session => session.id === activeSessionId);
    if (activeSession?.analysis && taskIsInProgress(activeSession.analysis.status)) {
      throw new Error('TASK_IN_PROGRESS');
    }
    const generation = generationRef.current;
    taskCreateInFlightRef.current = true;
    setBusy(true);
    setError(undefined);
    try {
      const task = await createTask(activeSessionId, { content });
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      const pendingMessage: Message = {
        id: task.trigger_message_id ?? `pending-${task.id}`,
        sender: 'user',
        text: content,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        taskId: task.id,
      };
      setSessions(current => current.map(session => session.id === activeSessionId ? {
        ...session,
        status: 'analyzing',
        messages: [...session.messages, pendingMessage],
        analysis: { taskId: task.id, status: task.status },
      } : session));
      setActiveTaskId(task.id);
      return task;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '保存消息失败');
      }
      throw reason;
    } finally {
      taskCreateInFlightRef.current = false;
      if (generationRef.current === generation) setBusy(false);
    }
  }, [activeSessionId, sessions, userId]);

  const retryMessage = useCallback(async (messageId: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    if (taskCreateInFlightRef.current) throw new Error('TASK_IN_PROGRESS');
    const activeSession = sessions.find(session => session.id === activeSessionId);
    const message = activeSession?.messages.find(item => item.id === messageId);
    if (!activeSession || !message?.taskId) throw new Error('RETRY_TASK_NOT_FOUND');
    const generation = generationRef.current;
    taskCreateInFlightRef.current = true;
    setBusy(true);
    setError(undefined);
    try {
      const task = await retryTask(message.taskId);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      setSessions(current => current.map(session => session.id === activeSession.id ? {
        ...session,
        status: 'analyzing',
        messages: session.messages.map(item => item.id === messageId ? { ...item, taskId: task.id } : item),
        analysis: { taskId: task.id, status: task.status },
        candidates: undefined,
        biReport: undefined,
      } : session));
      setActiveTaskId(task.id);
      return task;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '再次执行失败');
      }
      throw reason;
    } finally {
      taskCreateInFlightRef.current = false;
      if (generationRef.current === generation) setBusy(false);
    }
  }, [activeSessionId, sessions, userId]);

  useEffect(() => {
    if (!taskRuntime || !activeTaskId) return;
    const generation = generationRef.current;
    setSessions(current => current.map(session => session.analysis?.taskId === activeTaskId ? {
      ...session,
      status: taskRuntime.status === 'completed' || taskRuntime.status === 'completed_with_warnings'
        ? 'completed'
        : session.status,
      analysis: {
        ...session.analysis,
        status: taskRuntime.status ?? session.analysis.status,
        candidateVersion: taskRuntime.candidateVersion ?? session.analysis.candidateVersion,
        reportId: taskRuntime.visibleReportId ?? (
          taskRuntime.candidateVersion !== undefined
            && taskRuntime.candidateVersion !== session.analysis.candidateVersion
            ? undefined
            : session.analysis.reportId
        ),
      },
      biReport: taskRuntime.candidateVersion !== undefined
        && session.biReport?.candidate_version !== taskRuntime.candidateVersion
        ? undefined
        : session.biReport,
      candidates: taskRuntime.candidateVersion !== undefined
        && session.analysis.candidateVersion !== taskRuntime.candidateVersion
        ? undefined
        : session.candidates,
    } : session));
    if (taskRuntime.candidateVersion !== undefined) {
      const requestedTaskId = activeTaskId;
      const requestedCandidateVersion = taskRuntime.candidateVersion;
      void getCandidates(requestedTaskId)
        .then(page => {
          if (generationRef.current !== generation) return;
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
    if (taskRuntime.errorMessage && taskRuntime.errorMessageId) {
      const errorMessageId = taskRuntime.errorMessageId;
      setSessions(current => current.map(session => {
        if (session.analysis?.taskId !== activeTaskId || session.messages.some(message => message.id === errorMessageId)) {
          return session;
        }
        return {
          ...session,
          messages: [...session.messages, {
            id: errorMessageId,
            sender: 'ai' as const,
            text: taskRuntime.errorMessage ?? '分析任务执行失败，请稍后重试。',
            timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
          }],
        };
      }));
    }
    if (taskRuntime.visibleReportId) {
      const requestedTaskId = activeTaskId;
      const requestedCandidateVersion = taskRuntime.candidateVersion;
      const requestedReportId = taskRuntime.visibleReportId;
      void getReport(requestedReportId)
        .then(report => {
          if (generationRef.current !== generation) return;
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
  }, [activeTaskId, taskRuntime]);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );
  const isAnalyzing = busy || Boolean(
    activeSession?.analysis && taskIsInProgress(taskRuntime?.status ?? activeSession.analysis.status),
  );

  return {
    sessions,
    activeSession,
    activeSessionId,
    activeTaskId,
    taskRuntime,
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
