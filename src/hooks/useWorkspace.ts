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
import {
  cancelTask,
  createTask,
  createTurnId,
  getAnalysisReport,
  getTask,
  retryFollowups as retryFollowupsRequest,
  retryTask,
} from '../api/tasks';
import { getArtifactsSummary, markArtifactRead } from '../api/reports';
import type { ApiAnalysisReport, ArtifactModuleKey, CreateSessionInput } from '../api/contracts';
import { useTaskStream } from './useTaskStream';
import { useTaskFlows } from './useTaskFlows';
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

// 发起新任务时清空旧任务的分析报告；会话级报告（task_id 为 null，手动 KOL 分析）不随任务失效。
function reportAfterNewTask(session: Session): Session['analysisReport'] {
  return session.analysisReport?.task_id === null ? session.analysisReport : undefined;
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
  const [cancelRequested, setCancelRequested] = useState(false);
  const generationRef = useRef(0);
  const selectionRequestRef = useRef(0);
  const sessionsRef = useRef<Session[]>([]);
  const activeSessionIdRef = useRef<string>();
  const deletedSessionIdsRef = useRef(new Set<string>());
  const sessionOperationEpochsRef = useRef(new Map<string, number>());
  const taskCreateInFlightRef = useRef<TaskCreateLock | null>(null);
  const taskRuntime = useTaskStream(activeTaskId);
  const currentTaskRuntime = taskRuntime?.taskId === activeTaskId ? taskRuntime : undefined;

  // events 404（幽灵/已删除任务）是永久态：复位 activeTaskId，停止流订阅。
  useEffect(() => {
    if (!currentTaskRuntime?.notFound) return;
    setActiveTaskId(undefined);
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    // 幽灵任务（服务端不存在）：本地会话分析态置 failed，解除输入阻塞；
    // 下次 getSession 会以服务端真实状态为准。
    setSessions(current => current.map(session => (
      session.id === sessionId && session.analysis
        ? { ...session, analysis: { ...session.analysis, status: 'failed' } }
        : session
    )));
  }, [currentTaskRuntime?.notFound]);

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
    // 品牌/活动报告不挂载到「达人」Tab 的 KOL 分析子 Tab；report_type 缺省视为
    // kol_analysis（后端 AnalysisReportRead server_default 为 kol_analysis）。
    const matchingAnalysisReport = analysisReportResponse
      && (analysisReportResponse.report_type === undefined || analysisReportResponse.report_type === 'kol_analysis')
      && (analysisReportResponse.task_id === null || analysisReportResponse.task_id === analysis.taskId)
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
    const turnId = createTurnId();
    const optimisticMessage: Message = {
      id: `pending-turn-${turnId}`,
      sender: 'user',
      text: content,
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      turnId,
    };
    const taskCreateLock: TaskCreateLock = {
      sessionId: requestedSessionId,
      token: Symbol(requestedSessionId),
    };
    taskCreateInFlightRef.current = taskCreateLock;
    setBusy(true);
    setError(undefined);
    setSessions(current => current.map(session => session.id === requestedSessionId ? {
      ...session,
      messages: [...session.messages, optimisticMessage],
    } : session));
    try {
      if (activeSession && !isBrainstormProfileReady(activeSession)) {
        // 画像未 ready：走 brainstorm 澄清；ready 后可能绑定已创建任务，也可能继续接收 Planner 澄清。
        setIsClarifying(true);
        let response;
        try {
          response = await postBrainstorm(requestedSessionId, content, turnId);
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
          analysisReport: taskId ? reportAfterNewTask(session) : session.analysisReport,
        } : session));
        if (taskId && activeSessionIdRef.current === requestedSessionId) {
          setActiveTaskId(taskId);
        }
        return response;
      }
      const result = await createTask(requestedSessionId, { content, turn_id: turnId });
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      if (!sessionOperationIsCurrent(requestedSessionId, operationEpoch)) return result;
      if (result.outcome === 'clarify' || result.outcome === 'respond') {
        // planner 澄清/对话式回复：不落任务；保留乐观用户提问并追加 assistant 消息
        //（metadata.clarify.options 由 ChatArea 渲染为可点 chips；respond 为普通回复气泡）。
        const assistantMessage = toMessage(result.message);
        setSessions(current => current.map(session => session.id === requestedSessionId ? {
          ...session,
          messages: [...session.messages, assistantMessage],
        } : session));
        return result;
      }
      const task = result.task;
      setSessions(current => current.map(session => session.id === requestedSessionId ? {
        ...session,
        status: 'analyzing',
        messages: session.messages.map(message => message.id === optimisticMessage.id
          ? {
            ...message,
            id: task.trigger_message_id ?? message.id,
            taskId: task.id,
          }
          : message),
        analysis: { taskId: task.id, status: task.status, kind: task.kind },
        analysisReport: reportAfterNewTask(session),
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
        let persisted: Session | undefined;
        try {
          persisted = await getSession(requestedSessionId);
        } catch {
          // 后端状态无法读取时仍移除本地乐观消息，避免留下未确认内容。
        }
        if (
          generationRef.current === generation
          && sessionOperationIsCurrent(requestedSessionId, operationEpoch)
        ) {
          const persistedHasTurn = persisted?.messages.some(
            message => message.turnId === turnId,
          ) ?? false;
          setSessions(current => current.map(session => {
            if (session.id !== requestedSessionId) return session;
            if (persisted && persistedHasTurn) {
              return {
                ...persisted,
                artifactsSummary: session.artifactsSummary,
              };
            }
            return {
              ...session,
              messages: session.messages.filter(message => message.id !== optimisticMessage.id),
            };
          }));
          if (persistedHasTurn && activeSessionIdRef.current === requestedSessionId) {
            setActiveTaskId(persisted?.analysis?.taskId);
          }
          setError(reason instanceof Error ? reason.message : '保存消息失败');
        }
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
        analysisReport: reportAfterNewTask(session),
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

  // 取消当前任务：点击后立即 latch（按钮禁用），API 失败立即复位可重试；
  // 成功时不在此处复位，等 SSE task.cancelled 收敛为终态后由下方 effect 复位。
  const cancelActiveTask = useCallback(async () => {
    const taskId = activeTaskId;
    if (!taskId || cancelRequested) return;
    setCancelRequested(true);
    try {
      await cancelTask(taskId);
      // latch：不在此处复位，等 SSE task.cancelled（终态）由 effect 复位。
    } catch (cancelError) {
      console.warn('cancel task failed', cancelError);
      setCancelRequested(false);
    }
  }, [activeTaskId, cancelRequested]);

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
      // 任务级报告仅保留属于当前任务的（其余清空等待 report.updated 回填）；
      // 会话级报告（task_id 为 null，手动 KOL 分析）不随任务失效，始终保留。
      analysisReport: session.analysisReport
        && (session.analysisReport.task_id === null || session.analysisReport.task_id === activeTaskId)
        ? session.analysisReport
        : undefined,
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
          // 任务级报告必须属于当前任务；会话级报告（task_id 为 null，自动/手动
          // KOL 分析）始终接受，与 hydrateAnalysis/toSession 的口径一致。
          // 品牌/活动报告（report_type 非 kol_analysis）不挂载到 KOL 分析子 Tab，
          // report_type 缺省视为 kol_analysis。
          if (
            generationRef.current !== generation
            || (report.report_type !== undefined && report.report_type !== 'kol_analysis')
            || (report.task_id !== null && report.task_id !== requestedTaskId)
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

  // 每个任务一张执行流程卡：活跃任务终态冻结 + 历史任务事件回放（锚定在用户消息上）。
  const taskFlows = useTaskFlows(
    activeSessionId,
    activeSession?.messages ?? [],
    activeTaskId,
    currentTaskRuntime,
  );

  // 会话激活 / 任务状态变化（含终态）/ artifact.updated 到达时拉取 artifacts summary
  // （三 Tab 的最新产物与未读标记）。artifactUpdates 每次事件都是新对象引用。
  const runtimeArtifactUpdates = currentTaskRuntime?.artifactUpdates;
  const runtimeStatus = currentTaskRuntime?.status;
  useEffect(() => {
    if (!userId || !activeSessionId) return;
    const generation = generationRef.current;
    const operationEpoch = getSessionOperationEpoch(activeSessionId);
    void getArtifactsSummary(activeSessionId)
      .then(summary => {
        if (
          generationRef.current !== generation
          || !sessionOperationIsCurrent(activeSessionId, operationEpoch)
        ) return;
        setSessions(current => current.map(session => session.id === activeSessionId ? {
          ...session,
          artifactsSummary: summary,
        } : session));
      })
      .catch(() => undefined);
  }, [activeSessionId, runtimeStatus, runtimeArtifactUpdates, userId, getSessionOperationEpoch, sessionOperationIsCurrent]);

  // 点击 Tab 后标记模块已读：先调 api（失败保持未读），再本地清 unread。
  const markArtifactSeen = useCallback(async (moduleKey: ArtifactModuleKey, artifactId: string) => {
    if (!userId || !activeSessionId) return;
    try {
      await markArtifactRead(activeSessionId, moduleKey, artifactId);
    } catch {
      return;
    }
    setSessions(current => current.map(session => {
      if (session.id !== activeSessionId || !session.artifactsSummary) return session;
      const entry = session.artifactsSummary[moduleKey];
      if (!entry) return session;
      return {
        ...session,
        artifactsSummary: {
          ...session.artifactsSummary,
          [moduleKey]: { ...entry, unread: false },
        },
      };
    }));
  }, [activeSessionId, userId]);

  // 手动 KOL 分析（会话级报告，task_id 为 null）成功后，由面板回调写回会话状态。
  const setAnalysisReport = useCallback((sessionId: string, report: ApiAnalysisReport) => {
    setSessions(current => {
      const nextSessions = current.map(session => session.id === sessionId ? {
        ...session,
        analysis: session.analysis
          ? { ...session.analysis, analysisReportId: report.id }
          : session.analysis,
        analysisReport: report,
      } : session);
      sessionsRef.current = nextSessions;
      return nextSessions;
    });
  }, []);

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

  // cancelRequested latch 复位：终态到达 / 任务消失 / 不再 analyzing 三路。
  useEffect(() => {
    if (!cancelRequested) return;
    if (!activeTaskId || !isAnalyzing) {
      setCancelRequested(false);
      return;
    }
    const status = currentTaskRuntime?.status;
    if (status && isTerminalTaskStatus(status)) {
      setCancelRequested(false);
    }
  }, [cancelRequested, activeTaskId, isAnalyzing, currentTaskRuntime?.status]);

  return {
    sessions,
    activeSession,
    activeSessionId,
    activeTaskId,
    taskRuntime: currentTaskRuntime,
    taskFlows,
    loading,
    busy,
    isAnalyzing,
    isClarifying,
    cancelRequested,
    cancelActiveTask,
    error,
    reload,
    selectSession,
    createSession,
    updateSession,
    deleteSession,
    appendMessage,
    retryMessage,
    retryFollowups,
    setAnalysisReport,
    markArtifactSeen,
  };
}
