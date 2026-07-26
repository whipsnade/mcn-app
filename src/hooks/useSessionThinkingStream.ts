import { useEffect, useRef, useState } from 'react';

import { streamSessionThinking } from '../api/sessionThinking';
import {
  initialSessionThinking,
  reduceSessionThinking,
  type SessionThinkingRuntime,
} from '../state/sessionThinking';


const RECONNECT_DELAY_MS = 25;
const MAX_RECONNECT_DELAY_MS = 1000;

export function calculateSessionThinkingReconnectDelay(attempt: number): number {
  return Math.min(
    RECONNECT_DELAY_MS * 2 ** Math.max(0, attempt - 1),
    MAX_RECONNECT_DELAY_MS,
  );
}

export function useSessionThinkingStream(
  sessionId: string | undefined,
): SessionThinkingRuntime | undefined {
  const [runtime, setRuntime] = useState<SessionThinkingRuntime | undefined>();
  const latestState = useRef<SessionThinkingRuntime | undefined>();

  useEffect(() => {
    if (!sessionId) {
      latestState.current = undefined;
      setRuntime(undefined);
      return;
    }

    const controller = new AbortController();
    let stopped = false;
    let reconnectAttempt = 0;
    const initial = {
      ...initialSessionThinking(sessionId),
      connection: 'connecting' as const,
    };
    latestState.current = initial;
    setRuntime(initial);

    const update = (next: SessionThinkingRuntime) => {
      if (stopped || controller.signal.aborted) return;
      latestState.current = next;
      setRuntime(next);
    };
    const waitForReconnect = (delay: number) => new Promise<void>(resolve => {
      const timer = window.setTimeout(resolve, delay);
      controller.signal.addEventListener('abort', () => {
        window.clearTimeout(timer);
        resolve();
      }, { once: true });
    });
    const connect = async () => {
      while (!stopped && !controller.signal.aborted) {
        const current = latestState.current ?? initial;
        if (reconnectAttempt > 0) {
          update({ ...current, connection: 'reconnecting' });
        }
        try {
          await streamSessionThinking(
            sessionId,
            controller.signal,
            event => {
              if (stopped || controller.signal.aborted) return;
              const next = reduceSessionThinking(latestState.current ?? initial, event);
              update({ ...next, connection: 'connected' });
            },
            () => {
              const openedState = latestState.current ?? initial;
              update({ ...openedState, connection: 'connected' });
            },
          );
        } catch {
          if (stopped || controller.signal.aborted) break;
          const currentState = latestState.current ?? initial;
          update({ ...currentState, connection: 'error' });
        }
        if (stopped || controller.signal.aborted) break;
        reconnectAttempt += 1;
        await waitForReconnect(
          calculateSessionThinkingReconnectDelay(reconnectAttempt),
        );
      }
    };
    void connect();

    return () => {
      stopped = true;
      controller.abort();
    };
  }, [sessionId]);

  if (!sessionId) return undefined;
  if (!runtime || runtime.sessionId !== sessionId) {
    return {
      ...initialSessionThinking(sessionId),
      connection: 'connecting',
    };
  }
  return runtime;
}
