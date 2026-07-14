import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  appendMessage as appendSessionMessage,
  createSession as createSessionRequest,
  getSession,
  listSessions,
  updateSession as updateSessionRequest,
} from '../api/sessions';
import type { CreateSessionInput } from '../api/contracts';
import type { Session } from '../types';


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
  const generationRef = useRef(0);

  const load = useCallback(async (generation: number) => {
    setLoading(true);
    setError(undefined);
    try {
      const loaded = await listSessions();
      const first = loaded[0] ? await getSession(loaded[0].id) : undefined;
      if (generationRef.current !== generation) return;
      setSessions(first ? replaceSession(loaded, first) : loaded);
      setActiveSessionId(first?.id);
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
    setSessions([]);
    setActiveSessionId(undefined);
    setError(undefined);
    setLoading(false);
    setBusy(false);
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
      const loaded = await getSession(id);
      if (generationRef.current === generation) {
        setSessions(current => replaceSession(current, loaded));
      }
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '恢复会话失败');
      }
    }
  }, [userId]);

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
      const updated = await appendSessionMessage(activeSessionId, content);
      if (generationRef.current !== generation) throw new Error('STALE_WORKSPACE_REQUEST');
      setSessions(current => replaceSession(current, updated));
      return updated;
    } catch (reason) {
      if (generationRef.current === generation) {
        setError(reason instanceof Error ? reason.message : '保存消息失败');
      }
      throw reason;
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }, [activeSessionId, userId]);

  const activeSession = useMemo(
    () => sessions.find(session => session.id === activeSessionId),
    [activeSessionId, sessions],
  );

  return {
    sessions,
    activeSession,
    activeSessionId,
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
