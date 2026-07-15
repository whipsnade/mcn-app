import { useEffect, useRef, useState } from 'react';

import { streamTaskEvents } from '../api/taskStream';
import {
  initialTaskRuntime,
  isTerminalTaskStatus,
  reduceTaskEvent,
  type TaskRuntimeState,
} from '../state/taskEvents';


const RECONNECT_DELAY_MS = 25;
const MAX_RECONNECT_DELAY_MS = 500;

export interface TaskStreamOptions {
  random?: () => number;
}

export function calculateReconnectDelay(attempt: number, random: () => number = Math.random): number {
  const cap = Math.min(RECONNECT_DELAY_MS * 2 ** Math.max(0, attempt - 1), MAX_RECONNECT_DELAY_MS);
  const jitter = Math.min(1, Math.max(0, random()));
  return Math.round(cap * (0.5 + jitter * 0.5));
}

export function useTaskStream(
  taskId: string | undefined,
  options: TaskStreamOptions = {},
): TaskRuntimeState | undefined {
  const [runtime, setRuntime] = useState<TaskRuntimeState | undefined>();
  const latestState = useRef<TaskRuntimeState | undefined>();
  const randomRef = useRef(options.random ?? Math.random);
  randomRef.current = options.random ?? Math.random;

  useEffect(() => {
    if (!taskId) {
      latestState.current = undefined;
      setRuntime(undefined);
      return;
    }
    const controller = new AbortController();
    let stopped = false;
    let attempts = 0;
    const initial = initialTaskRuntime(taskId);
    latestState.current = initial;
    setRuntime(initial);

    const update = (next: TaskRuntimeState) => {
      latestState.current = next;
      setRuntime(next);
    };
    const waitForReconnect = (delay: number) => new Promise<void>(resolve => {
      window.setTimeout(resolve, delay);
    });
    const connect = async () => {
      while (!stopped && !controller.signal.aborted) {
        const current = latestState.current ?? initial;
        update({ ...current, connection: attempts === 0 ? 'connecting' : 'reconnecting' });
        try {
          await streamTaskEvents(taskId, current.lastEventId, controller.signal, event => {
            const next = reduceTaskEvent(latestState.current ?? initial, event);
            update({ ...next, connection: isTerminalTaskStatus(next.status) ? 'closed' : 'connected' });
          });
        } catch (error) {
          if (controller.signal.aborted || stopped) break;
          const currentState = latestState.current ?? initial;
          update({ ...currentState, connection: 'error' });
        }
        const currentState = latestState.current ?? initial;
        if (stopped || controller.signal.aborted || isTerminalTaskStatus(currentState.status)) break;
        attempts += 1;
        await waitForReconnect(calculateReconnectDelay(attempts, randomRef.current));
      }
    };
    void connect();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [taskId]);

  return runtime;
}
