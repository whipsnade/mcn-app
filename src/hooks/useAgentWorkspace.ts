import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type {
  AgentKolDetailResponse,
  AgentKolDetailSelectionRef,
  ApiAgentMessage,
  ApiAgentRun,
  ApiAgentSession,
  ApiAgentSessionDetail,
} from '../api/agent';
import {
  cancelRun as cancelRunRequest,
  createKolDetailRun as createKolDetailRunRequest,
  createSession as createSessionRequest,
  deleteSession as deleteSessionRequest,
  getSession,
  listSessions,
  patchSession as patchSessionRequest,
  resumeRun as resumeRunRequest,
  sendMessage as sendMessageRequest,
} from '../api/agent';
import type { ApiAgentArtifact } from '../api/agentArtifacts';
import { listArtifacts, markArtifactRead } from '../api/agentArtifacts';
import type { ApiWallet } from '../api/contracts';
import { getWallet } from '../api/wallet';
import { isTerminalRunStatus } from '../state/agentEvents';
import type { RunRuntimeState } from '../state/agentEvents';
import { useAgentRun } from './useAgentRun';


export interface AgentWorkspaceSession {
  id: string;
  title: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  runs: ApiAgentRun[];
  messages: ApiAgentMessage[];
  latestRunId?: string;
}

function toSummarySession(source: ApiAgentSession): AgentWorkspaceSession {
  return {
    id: source.id,
    title: source.title,
    status: source.status,
    createdAt: source.created_at,
    updatedAt: source.updated_at,
    runs: [],
    messages: [],
  };
}

// Run 恢复锚点（§6.4）：优先当前活动（queued/running/reviewing/paused）的用户可见
// Run，否则取服务端顺序（created_at 升序 + id tie-break）的最后一个用户可见 Run。
// 服务端已过滤 visibility=user；不再用随机 uuid 顺序推断最新 Run，kol_detail
// 辅助 Run 与用户主 Run 共用同一锚定语义。
const ANCHOR_ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'reviewing', 'paused']);

function pickAnchorRunId(runs: ApiAgentRun[]): string | undefined {
  for (let index = runs.length - 1; index >= 0; index -= 1) {
    if (ANCHOR_ACTIVE_RUN_STATUSES.has(runs[index].status)) return runs[index].id;
  }
  return runs.at(-1)?.id;
}

function toWorkspaceSession(detail: ApiAgentSessionDetail): AgentWorkspaceSession {
  return {
    id: detail.id,
    title: detail.title,
    status: detail.status,
    createdAt: detail.created_at,
    updatedAt: detail.updated_at,
    runs: detail.runs,
    messages: detail.messages,
    latestRunId: pickAnchorRunId(detail.runs),
  };
}

function replaceSession(sessions: AgentWorkspaceSession[], next: AgentWorkspaceSession): AgentWorkspaceSession[] {
  const exists = sessions.some(session => session.id === next.id);
  if (!exists) return [next, ...sessions];
  return sessions.map(session => session.id === next.id ? next : session);
}

// 会话详情需要回拉同步的「稳定态」：终态（含积分结算）+ 暂停 + 澄清等待。
// running/reviewing/queued 期间不打扰（由 Run SSE 驱动实时卡）。
function isSettledRunStatus(status: string | undefined): boolean {
  return isTerminalRunStatus(status)
    || status === 'paused'
    || status === 'clarification_requested';
}

// utility 建议在 Run 终态 settle 后异步落库（fire-and-forget 模型调用），即时回拉
// 通常早于 suggestions 写入；缺失时补一次延迟回拉（一次性，此后等下次自然刷新）。
const SUGGESTIONS_REFETCH_DELAY_MS = 5_000;

// utility 建议落父 Run 最新 assistant 消息的 metadata.suggestions（§6.4）。
function hasRunSuggestions(detail: ApiAgentSessionDetail, runId: string): boolean {
  return detail.messages.some(message => (
    message.run_id === runId
    && message.role === 'assistant'
    && (message.metadata?.suggestions?.length ?? 0) > 0
  ));
}

export function useAgentWorkspace(userId?: string) {
  const [sessions, setSessions] = useState<AgentWorkspaceSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>();
  const [activeRunId, setActiveRunId] = useState<string>();
  const [artifacts, setArtifacts] = useState<ApiAgentArtifact[]>([]);
  const [wallet, setWallet] = useState<ApiWallet>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const generationRef = useRef(0);
  const sessionsRef = useRef<AgentWorkspaceSession[]>([]);
  const activeSessionIdRef = useRef<string>();
  const deletedSessionIdsRef = useRef(new Set<string>());
  // Run 稳定态回拉会话详情去重键：(sessionId, runId, status) 相同不再重复拉取。
  const lastSessionRefetchRef = useRef<{ sessionId?: string; runId?: string; status?: string }>({});
  // utility 建议延迟补拉定时器：组件卸载 / 用户切换时清理，避免悬挂回调。
  const suggestionRetryTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const run: RunRuntimeState | undefined = useAgentRun(activeRunId);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );

  const load = useCallback(async (generation: number) => {
    setLoading(true);
    setError(undefined);
    try {
      const loaded = (await listSessions()).filter(session => !deletedSessionIdsRef.current.has(session.id));
      const first = loaded[0];
      const detail = first ? await getSession(first.id) : undefined;
      if (generationRef.current !== generation) return;
      const nextSessions = loaded.map(item => (
        detail && item.id === detail.id ? toWorkspaceSession(detail) : toSummarySession(item)
      ));
      sessionsRef.current = nextSessions;
      setSessions(nextSessions);
      const active = detail ? toWorkspaceSession(detail) : nextSessions[0];
      activeSessionIdRef.current = active?.id;
      setActiveSessionId(active?.id);
      setActiveRunId(active?.latestRunId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '加载会话失败');
      }
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const generation = ++generationRef.current;
    sessionsRef.current = [];
    activeSessionIdRef.current = undefined;
    deletedSessionIdsRef.current.clear();
    setSessions([]);
    setActiveSessionId(undefined);
    setActiveRunId(undefined);
    setArtifacts([]);
    setError(undefined);
    setLoading(false);
    setBusy(false);
    if (userId) void load(generation);
    return () => {
      if (generationRef.current === generation) generationRef.current += 1;
      if (suggestionRetryTimerRef.current) {
        clearTimeout(suggestionRetryTimerRef.current);
        suggestionRetryTimerRef.current = undefined;
      }
    };
  }, [load, userId]);

  const reload = useCallback(async () => {
    if (!userId) return;
    await load(generationRef.current);
  }, [load, userId]);

  const selectSession = useCallback(async (id: string) => {
    if (!userId) return;
    const generation = generationRef.current;
    setError(undefined);
    try {
      const detail = await getSession(id);
      if (generationRef.current !== generation) return;
      const session = toWorkspaceSession(detail);
      sessionsRef.current = replaceSession(sessionsRef.current, session);
      setSessions(sessionsRef.current);
      activeSessionIdRef.current = id;
      setActiveSessionId(id);
      setActiveRunId(session.latestRunId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '恢复会话失败');
      }
    }
  }, [userId]);

  const createSession = useCallback(async (title?: string): Promise<AgentWorkspaceSession> => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const created = await createSessionRequest(title);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      const session = toSummarySession(created);
      sessionsRef.current = replaceSession(sessionsRef.current, session);
      setSessions(sessionsRef.current);
      activeSessionIdRef.current = created.id;
      setActiveSessionId(created.id);
      setActiveRunId(undefined);
      return session;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '创建会话失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [userId]);

  const renameSession = useCallback(async (id: string, title: string) => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const updated = await patchSessionRequest(id, { title });
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      sessionsRef.current = sessionsRef.current.map(session => session.id === id ? {
        ...session,
        title: updated.title,
        updatedAt: updated.updated_at,
      } : session);
      setSessions(sessionsRef.current);
      return updated;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '重命名会话失败');
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
      deletedSessionIdsRef.current.add(id);
      const remaining = sessionsRef.current.filter(session => session.id !== id);
      sessionsRef.current = remaining;
      setSessions(remaining);
      if (activeSessionIdRef.current !== id) return;
      const next = remaining[0];
      if (!next) {
        activeSessionIdRef.current = undefined;
        setActiveSessionId(undefined);
        setActiveRunId(undefined);
        return;
      }
      await selectSession(next.id);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '删除会话失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [selectSession, userId]);

  const sendMessage = useCallback(async (sessionId: string, content: string): Promise<string> => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    // 乐观插入：用户消息立即出现在会话流，响应成功回填 run_id 以锚定执行卡；
    // assistant 回复在 Run 进入稳定态后由下方 effect 回拉会话详情补充。
    const optimisticId = `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const optimisticMessage: ApiAgentMessage = {
      id: optimisticId,
      role: 'user',
      content,
      sequence: 0,
      run_id: null,
      created_at: new Date().toISOString(),
    };
    const patchMessages = (sessionIdToPatch: string, patch: (messages: ApiAgentMessage[]) => ApiAgentMessage[]) => {
      sessionsRef.current = sessionsRef.current.map(session => session.id === sessionIdToPatch
        ? { ...session, messages: patch(session.messages) }
        : session);
      setSessions(sessionsRef.current);
    };
    patchMessages(sessionId, messages => [...messages, optimisticMessage]);
    try {
      const response = await sendMessageRequest(sessionId, content);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      // 回填 run_id：执行卡锚定到触发它的用户消息下方。
      patchMessages(sessionId, messages => messages.map(message => message.id === optimisticId
        ? { ...message, run_id: response.run_id }
        : message));
      if (activeSessionIdRef.current === sessionId) setActiveRunId(response.run_id);
      return response.run_id;
    } catch (reason) {
      // 失败移除乐观消息，避免留下未确认内容。
      patchMessages(sessionId, messages => messages.filter(message => message.id !== optimisticId));
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '发送消息失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [userId]);

  const cancelActiveRun = useCallback(async () => {
    if (!activeRunId) return;
    const generation = generationRef.current;
    try {
      await cancelRunRequest(activeRunId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '取消运行失败');
      }
    }
  }, [activeRunId]);

  const resumeActiveRun = useCallback(async () => {
    if (!activeRunId) return;
    const generation = generationRef.current;
    try {
      await resumeRunRequest(activeRunId);
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '恢复运行失败');
      }
    }
  }, [activeRunId]);

  const createKolDetail = useCallback(async (
    sessionId: string,
    platform: string,
    kolUid: string,
    selectionRef?: AgentKolDetailSelectionRef,
  ): Promise<AgentKolDetailResponse> => {
    if (!userId) throw new Error('AUTH_EXPIRED');
    const generation = generationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const result = await createKolDetailRunRequest(sessionId, platform, kolUid, selectionRef);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      if (result.run_id && activeSessionIdRef.current === sessionId) setActiveRunId(result.run_id);
      return result;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '创建达人详情失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [userId]);

  const refreshWallet = useCallback(async () => {
    if (!userId) return;
    try {
      setWallet(await getWallet());
    } catch {
      // 钱包刷新尽力而为：保持上一次余额。
    }
  }, [userId]);

  // 会话激活 / Run 产物相关事件（draft/review/publish）到达时刷新当前会话的
  // artifact 目录（BI 三 Tab）。只依赖 artifactsVersion：纯 thinking/tool 增量
  // 不会触发整目录重拉。
  useEffect(() => {
    if (!userId || !activeSessionId) return;
    const generation = generationRef.current;
    void listArtifacts(activeSessionId)
      .then(items => {
        if (generationRef.current !== generation) return;
        setArtifacts(items);
      })
      .catch(() => undefined);
  }, [activeSessionId, run?.artifactsVersion, userId]);

  // Run 进入稳定态（终态 / paused / 澄清等待）后重新拉取会话详情：让 assistant 回复
  // （含澄清消息）出现在消息流，同时把乐观用户消息替换为服务端持久化版本。
  useEffect(() => {
    if (!userId || !activeSessionId || !run) return;
    const status = run.status;
    if (!isSettledRunStatus(status)) return;
    const last = lastSessionRefetchRef.current;
    if (last.sessionId === activeSessionId && last.runId === run.runId && last.status === status) return;
    lastSessionRefetchRef.current = { sessionId: activeSessionId, runId: run.runId, status };
    const generation = generationRef.current;
    const requestedSessionId = activeSessionId;
    const requestedRunId = run.runId;
    const applyDetail = (detail: ApiAgentSessionDetail) => {
      const session = toWorkspaceSession(detail);
      sessionsRef.current = replaceSession(sessionsRef.current, session);
      setSessions(sessionsRef.current);
    };
    void getSession(requestedSessionId)
      .then(detail => {
        if (generationRef.current !== generation) return;
        applyDetail(detail);
        // utility 建议异步落库：即时回拉通常早于 suggestions 写入，缺失时补一次
        // 延迟回拉（一次性）；卸载/切会话由 generation 与 activeSessionIdRef 兜底。
        if (hasRunSuggestions(detail, requestedRunId)) return;
        if (suggestionRetryTimerRef.current) clearTimeout(suggestionRetryTimerRef.current);
        suggestionRetryTimerRef.current = setTimeout(() => {
          suggestionRetryTimerRef.current = undefined;
          void getSession(requestedSessionId)
            .then(latest => {
              if (generationRef.current !== generation) return;
              if (activeSessionIdRef.current !== requestedSessionId) return;
              applyDetail(latest);
            })
            .catch(() => undefined);
        }, SUGGESTIONS_REFETCH_DELAY_MS);
      })
      .catch(() => undefined);
  }, [activeSessionId, run, userId]);

  // 加载后 + Run 到达终态（积分结算）时刷新钱包余额；
  // 只在终态翻转时刷新，不在 running/reviewing 之间反复拉取。
  useEffect(() => {
    if (!userId) return;
    void refreshWallet();
  }, [refreshWallet, userId, isTerminalRunStatus(run?.status)]);

  // run events 404/4xx（已删除/不可见）是永久态：复位 activeRunId，停止流订阅。
  useEffect(() => {
    if (!run?.notFound) return;
    setActiveRunId(undefined);
  }, [run?.notFound]);

  const markArtifactSeen = useCallback(async (module: string, lastSeenSequence: number) => {
    if (!userId || !activeSessionId) return;
    try {
      await markArtifactRead(activeSessionId, module, lastSeenSequence);
    } catch {
      // 失败保持未读，由下次事件刷新兜底。
    }
  }, [activeSessionId, userId]);

  return {
    sessions,
    activeSession,
    activeSessionId,
    activeRunId,
    run,
    artifacts,
    wallet,
    loading,
    busy,
    error,
    reload,
    selectSession,
    createSession,
    renameSession,
    deleteSession,
    sendMessage,
    cancelActiveRun,
    resumeActiveRun,
    createKolDetail,
    refreshWallet,
    markArtifactSeen,
  };
}
