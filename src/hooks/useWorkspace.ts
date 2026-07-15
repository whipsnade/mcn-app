import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  createSession as createSessionRequest,
  getSession,
  listSessions,
  updateSession as updateSessionRequest,
} from '../api/sessions';
import { createTask, getCandidates, getReport } from '../api/tasks';
import type { CreateSessionInput } from '../api/contracts';
import { useTaskStream } from './useTaskStream';
import type { Message, Session } from '../types';


function replaceSession(sessions: Session[], nextSession: Session): Session[] {
  const exists = sessions.some(session => session.id === nextSession.id);
  if (!exists) {
    return [nextSession, ...sessions];
  }
  return sessions.map(session => session.id === nextSession.id ? nextSession : session);
}


export function useWorkspace(userId?: string) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [activeTaskId, setActiveTaskId] = useState<string>();
  const generationRef = useRef(0);
  const taskRuntime = useTaskStream(activeTaskId);

  const hydrateAnalysis = useCallback(async (session: Session, generation: number): Promise<Session> => {
    const analysis = session.analysis;
    if (!analysis || generationRef.current !== generation) return session;
    const [candidatePage, report] = await Promise.all([
      analysis.candidateVersion === undefined ? Promise.resolve(undefined) : getCandidates(analysis.taskId),
      analysis.reportId === undefined ? Promise.resolve(undefined) : getReport(analysis.reportId),
    ]);
    if (generationRef.current !== generation) return session;
    return {
      ...session,
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
      })),
      biReport: report ? {
        id: report.id,
        taskId: report.task_id,
        reportVersion: report.report_version,
        candidateVersion: report.candidate_version,
        overview: report.overview,
        conclusion: report.conclusion,
        generatedAt: report.generated_at,
      } : undefined,
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
      setSessions(first ? replaceSession(loaded, first) : loaded);
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
    setSessions([]);
    setActiveSessionId(undefined);
    setError(undefined);
    setLoading(false);
    setBusy(false);
    setActiveTaskId(undefined);
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
    setActiveSessionId(id);
    setError(undefined);
    try {
      const loaded = await hydrateAnalysis(await getSession(id), generation);
      if (generationRef.current === generation) {
        setSessions(current => replaceSession(current, loaded));
        setActiveTaskId(loaded.analysis?.taskId);
      }
    } catch (reason) {
      if (generationRef.current === generation) {
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
      setSessions(current => replaceSession(current, created));
      setActiveSessionId(created.id);
      setActiveTaskId(undefined);
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
      setSessions(current => replaceSession(current, updated));
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

  const appendMessage = useCallback(async (content: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    if (!activeSessionId) return;
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const task = await createTask(activeSessionId, { content });
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      const pendingMessage: Message = {
        id: `pending-${task.id}`,
        sender: 'user',
        text: content,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
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
      if (generationRef.current === generation) setBusy(false);
    }
  }, [activeSessionId, userId]);

  useEffect(() => {
    if (!taskRuntime || !activeTaskId) return;
    const generation = generationRef.current;
    setSessions(current => current.map(session => session.analysis?.taskId === activeTaskId ? {
      ...session,
      status: taskRuntime.status === 'completed' ? 'completed' : session.status,
      analysis: {
        ...session.analysis,
        status: taskRuntime.status ?? session.analysis.status,
        candidateVersion: taskRuntime.candidateVersion ?? session.analysis.candidateVersion,
        reportId: taskRuntime.visibleReportId ?? session.analysis.reportId,
      },
    } : session));
    if (taskRuntime.candidateVersion !== undefined) {
      void getCandidates(activeTaskId)
        .then(page => {
          if (generationRef.current !== generation) return;
          setSessions(current => current.map(session => session.analysis?.taskId === activeTaskId ? {
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
            })),
          } : session));
        })
        .catch(() => undefined);
    }
    if (taskRuntime.visibleReportId) {
      void getReport(taskRuntime.visibleReportId)
        .then(report => {
          if (generationRef.current !== generation) return;
          setSessions(current => current.map(session => session.analysis?.taskId === activeTaskId ? {
            ...session,
            biReport: {
              id: report.id,
              taskId: report.task_id,
              reportVersion: report.report_version,
              candidateVersion: report.candidate_version,
              overview: report.overview,
              conclusion: report.conclusion,
              generatedAt: report.generated_at,
            },
          } : session));
        })
        .catch(() => undefined);
    }
  }, [activeTaskId, taskRuntime]);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );

  return {
    sessions,
    activeSession,
    activeSessionId,
    activeTaskId,
    taskRuntime,
    loading,
    busy,
    error,
    reload,
    selectSession,
    createSession,
    updateSession,
    appendMessage,
  };
}
