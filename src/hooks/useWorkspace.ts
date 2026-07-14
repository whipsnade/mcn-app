import { useCallback, useEffect, useMemo, useState } from 'react';

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


export function useWorkspace(enabled: boolean) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const reload = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const loaded = await listSessions();
      const first = loaded[0] ? await getSession(loaded[0].id) : undefined;
      setSessions(first ? replaceSession(loaded, first) : loaded);
      setActiveSessionId(first?.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '加载会话失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setSessions([]);
      setActiveSessionId(undefined);
      setError(undefined);
      setLoading(false);
      return;
    }
    void reload();
  }, [enabled, reload]);

  const selectSession = useCallback(async (id: string) => {
    setActiveSessionId(id);
    setError(undefined);
    try {
      const loaded = await getSession(id);
      setSessions(current => replaceSession(current, loaded));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '恢复会话失败');
    }
  }, []);

  const createSession = useCallback(async (input: CreateSessionInput) => {
    setBusy(true);
    setError(undefined);
    try {
      const created = await createSessionRequest(input);
      setSessions(current => replaceSession(current, created));
      setActiveSessionId(created.id);
      return created;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建会话失败');
      throw reason;
    } finally {
      setBusy(false);
    }
  }, []);

  const updateSession = useCallback(async (id: string, changes: Record<string, unknown>) => {
    setBusy(true);
    setError(undefined);
    try {
      const updated = await updateSessionRequest(id, changes);
      setSessions(current => replaceSession(current, updated));
      return updated;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '更新会话失败');
      throw reason;
    } finally {
      setBusy(false);
    }
  }, []);

  const appendMessage = useCallback(async (content: string) => {
    if (!activeSessionId) {
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      const updated = await appendSessionMessage(activeSessionId, content);
      setSessions(current => replaceSession(current, updated));
      return updated;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存消息失败');
      throw reason;
    } finally {
      setBusy(false);
    }
  }, [activeSessionId]);

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
