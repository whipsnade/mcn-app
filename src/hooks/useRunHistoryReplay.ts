/** 历史 Run 事件回放（C3：历史 Run 完整回放）。
 *
 * 会话详情只带 Run 元数据；本 hook 对每个「终态且非活动」的历史 Run 从
 * events 端点全量回放一次（后端在终态事件后自然收流，单次 fetch 即可取完），
 * 经 ``reduceRunEvent`` 幂等归并为完整 Run 卡状态（thinking/工具步骤/Draft/
 * Review/终态）。活动 Run 仍走 ``useAgentRun`` 实时订阅，不在此重复拉取。
 *
 * 预算与降级：
 * - 并发上限 ``HISTORY_REPLAY_CONCURRENCY``，超出的 Run 排队补位；
 * - 每个 runId 每会话只回放一次（settle 回拉会话详情不会触发重复回放）；
 * - 回放失败（404/网络/中止）保留元数据空壳卡，不阻塞会话加载；
 * - 非终态 Run（paused/澄清等待等）不回放：events 流只在终态事件后收口，
 *   对 paused Run 全量回放会悬挂。paused 语义由锚点逻辑覆盖——最新 paused
 *   Run 是活动 Run（走实时订阅并保留「继续」按钮）；非锚点 paused Run
 *   保留元数据卡。
 * - 内存：thinking 逐 delta 累积与实时路径同构，reducer 按 sequence 幂等，
 *   单 Run 事件量受后端 attempt/决策上限约束，不做额外截断。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { fetchRunEvents, type ApiAgentRun } from '../api/agent';
import {
  initialRunRuntime,
  isTerminalRunStatus,
  reduceRunEvent,
  type RunRuntimeState,
  type RunStatus,
} from '../state/agentEvents';


/** 历史 Run 回放并发上限：超出排队，避免多轮会话一次性打满连接。 */
export const HISTORY_REPLAY_CONCURRENCY = 3;

/** 无 Run 会话的共享空数组：保持引用稳定，避免回放 effect 无谓重跑。 */
export const NO_RUNS: readonly ApiAgentRun[] = [];

/** 由会话详情 Run 元数据构造的空壳卡（回放前的即时占位 / 回放失败的降级态）。 */
export function seedRunRuntime(run: ApiAgentRun): RunRuntimeState {
  return {
    ...initialRunRuntime(run.id),
    connection: 'closed',
    status: run.status as RunStatus,
  };
}

async function replayRunEvents(run: ApiAgentRun, signal: AbortSignal): Promise<RunRuntimeState> {
  const seed = seedRunRuntime(run);
  let state = seed;
  try {
    await fetchRunEvents(run.id, 0, signal, event => {
      state = reduceRunEvent(state, event);
    });
  } catch {
    // 回放失败（404/网络错误/会话切换中止）降级为元数据空壳卡。
    return seed;
  }
  return { ...state, connection: 'closed' };
}

export function useRunHistoryReplay(
  sessionId: string | undefined,
  runs: readonly ApiAgentRun[],
  activeRunId: string | undefined,
): Record<string, RunRuntimeState> {
  const [replayed, setReplayed] = useState<Record<string, RunRuntimeState>>({});
  const sessionRef = useRef<string>();
  const generationRef = useRef(0);
  const doneRef = useRef(new Set<string>());
  const inFlightRef = useRef(new Set<string>());
  const queueRef = useRef<ApiAgentRun[]>([]);
  const abortsRef = useRef(new Map<string, AbortController>());

  // 会话切换/卸载：中止在途回放、清空队列与结果，从新会话重新播种。
  useEffect(() => {
    if (sessionRef.current === sessionId) return;
    sessionRef.current = sessionId;
    generationRef.current += 1;
    for (const controller of abortsRef.current.values()) controller.abort();
    abortsRef.current.clear();
    doneRef.current.clear();
    inFlightRef.current.clear();
    queueRef.current = [];
    setReplayed({});
  }, [sessionId]);

  useEffect(() => () => {
    generationRef.current += 1;
    for (const controller of abortsRef.current.values()) controller.abort();
    abortsRef.current.clear();
  }, []);

  // 终态且非活动的 Run 入队回放；并发上限内补位推进。
  useEffect(() => {
    if (!sessionId) return;
    const generation = generationRef.current;
    for (const run of runs) {
      if (run.id === activeRunId) continue;
      if (!isTerminalRunStatus(run.status)) continue;
      if (doneRef.current.has(run.id) || inFlightRef.current.has(run.id)) continue;
      if (queueRef.current.some(queued => queued.id === run.id)) continue;
      queueRef.current.push(run);
    }
    const pump = () => {
      while (inFlightRef.current.size < HISTORY_REPLAY_CONCURRENCY && queueRef.current.length > 0) {
        const run = queueRef.current.shift();
        if (!run) break;
        inFlightRef.current.add(run.id);
        const controller = new AbortController();
        abortsRef.current.set(run.id, controller);
        void replayRunEvents(run, controller.signal)
          .then(state => {
            if (generationRef.current !== generation || controller.signal.aborted) return;
            setReplayed(current => ({ ...current, [run.id]: state }));
          })
          .finally(() => {
            inFlightRef.current.delete(run.id);
            doneRef.current.add(run.id);
            abortsRef.current.delete(run.id);
            if (generationRef.current === generation) pump();
          });
      }
    };
    pump();
  }, [sessionId, runs, activeRunId]);

  // 所有 Run 先给元数据空壳卡（立即渲染，不再停留在「Run 加载中…」），
  // 回放完成后以完整状态覆盖。
  const seeds = useMemo(() => {
    const map: Record<string, RunRuntimeState> = {};
    for (const run of runs) map[run.id] = seedRunRuntime(run);
    return map;
  }, [runs]);

  return useMemo(() => ({ ...seeds, ...replayed }), [seeds, replayed]);
}
