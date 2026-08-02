import { useEffect, useRef, useState } from 'react';

import { fetchRunEvents } from '../api/agent';
import {
  initialRunRuntime,
  isTerminalRunStatus,
  reduceRunEvent,
  type RunRuntimeState,
} from '../state/agentEvents';


const RECONNECT_DELAY_MS = 25;
const MAX_RECONNECT_DELAY_MS = 500;

export interface AgentRunOptions {
  random?: () => number;
}

export function calculateRunReconnectDelay(
  attempt: number,
  random: () => number = Math.random,
): number {
  const cap = Math.min(
    RECONNECT_DELAY_MS * 2 ** Math.max(0, attempt - 1),
    MAX_RECONNECT_DELAY_MS,
  );
  const jitter = Math.min(1, Math.max(0, random()));
  return Math.round(cap * (0.5 + jitter * 0.5));
}

/** 4xx 是永久失败：Run 不存在/不可见（404）或请求被拒，重连不会成功。 */
function isPermanentSseError(error: unknown): boolean {
  return error instanceof Error && /^SSE_4\d{2}$/.test(error.message);
}

export function useAgentRun(
  runId: string | undefined,
  options: AgentRunOptions = {},
): RunRuntimeState | undefined {
  const [runtime, setRuntime] = useState<RunRuntimeState | undefined>();
  const latestState = useRef<RunRuntimeState | undefined>();
  // 代际令牌：runId 切换（含会话切换）时递增，丢弃旧订阅迟到的响应。
  const generationRef = useRef(0);
  const randomRef = useRef(options.random ?? Math.random);
  randomRef.current = options.random ?? Math.random;

  useEffect(() => {
    if (!runId) {
      latestState.current = undefined;
      setRuntime(undefined);
      return;
    }

    const controller = new AbortController();
    let stopped = false;
    let attempts = 0;
    const generation = ++generationRef.current;
    const initial = initialRunRuntime(runId);
    latestState.current = initial;
    setRuntime(initial);

    const update = (next: RunRuntimeState) => {
      if (stopped || controller.signal.aborted || generation !== generationRef.current) return;
      latestState.current = next;
      setRuntime(next);
    };
    const waitForReconnect = (delay: number) => new Promise<void>(resolve => {
      let settled = false;
      const complete = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        controller.signal.removeEventListener('abort', complete);
        resolve();
      };
      const timer = window.setTimeout(complete, delay);
      controller.signal.addEventListener('abort', complete, { once: true });
    });
    const connect = async () => {
      while (!stopped && !controller.signal.aborted && generation === generationRef.current) {
        const current = latestState.current ?? initial;
        update({ ...current, connection: attempts === 0 ? 'connecting' : 'reconnecting' });
        try {
          await fetchRunEvents(runId, current.lastEventId, controller.signal, event => {
            const next = reduceRunEvent(latestState.current ?? initial, event);
            update({ ...next, connection: isTerminalRunStatus(next.status) ? 'closed' : 'connected' });
          });
        } catch (error) {
          if (controller.signal.aborted || stopped || generation !== generationRef.current) break;
          const currentState = latestState.current ?? initial;
          if (isPermanentSseError(error)) {
            // Run 不存在/不可见（404）或请求被拒（其它 4xx）：永久态，
            // 停止重连并标记，交由上层复位 activeRunId。
            update({ ...currentState, connection: 'closed', notFound: true });
            break;
          }
          update({ ...currentState, connection: 'error' });
        }
        const currentState = latestState.current ?? initial;
        if (
          stopped
          || controller.signal.aborted
          || generation !== generationRef.current
          || isTerminalRunStatus(currentState.status)
        ) break;
        attempts += 1;
        await waitForReconnect(calculateRunReconnectDelay(attempts, randomRef.current));
      }
    };
    void connect();

    return () => {
      stopped = true;
      generationRef.current += 1;
      controller.abort();
    };
  }, [runId]);

  return runtime;
}
