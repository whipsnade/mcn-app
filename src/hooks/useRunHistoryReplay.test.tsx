import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentRun } from '../api/agent';
import * as agentApi from '../api/agent';
import type { RunEvent } from '../state/agentEvents';
import { HISTORY_REPLAY_CONCURRENCY, useRunHistoryReplay } from './useRunHistoryReplay';

vi.mock('../api/agent', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/agent')>();
  return { ...actual, fetchRunEvents: vi.fn() };
});

const mockFetchRunEvents = vi.mocked(agentApi.fetchRunEvents);

function makeRun(id: string, status: string): ApiAgentRun {
  return {
    id,
    session_id: 's1',
    parent_run_id: null,
    profile_name: 'session_analyst_v1',
    status,
    cancel_requested: false,
    outcome: null,
    decision_count: 1,
    review_count: 0,
    revision_count: 0,
    error_code: null,
    started_at: '2026-08-01T10:00:00',
    paused_at: null,
    completed_at: null,
  };
}

/** fetchRunEvents 假实现：把 events 逐个推给 onEvent 后收流（对齐终态回放语义）。 */
function stubReplay(eventsByRun: Record<string, Array<Omit<RunEvent, 'runId'>>>) {
  mockFetchRunEvents.mockImplementation(async (runId, _lastEventId, _signal, onEvent) => {
    for (const event of eventsByRun[runId] ?? []) {
      onEvent({ ...event, runId });
    }
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('useRunHistoryReplay', () => {
  it('seeds shell cards for every run before replay completes', async () => {
    // 回放悬挂（永不收流）时也要先渲染元数据空壳卡，不阻塞会话加载。
    mockFetchRunEvents.mockReturnValue(new Promise<void>(() => {}));
    const runs = [makeRun('run-1', 'completed'), makeRun('run-2', 'failed')];

    const { result } = renderHook(() => useRunHistoryReplay('s1', runs, undefined));

    expect(result.current['run-1']).toMatchObject({ status: 'completed', steps: [], toolCalls: [] });
    expect(result.current['run-2']).toMatchObject({ status: 'failed', steps: [], toolCalls: [] });
  });

  it('replays a terminal run into a full card with thinking, tools and steps', async () => {
    stubReplay({
      'run-1': [
        { id: 1, type: 'run.started', payload: {} },
        { id: 2, type: 'thinking.started', payload: {} },
        { id: 3, type: 'thinking.delta', payload: { text: '正在检索品牌' } },
        { id: 4, type: 'thinking.completed', payload: {} },
        { id: 5, type: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
        { id: 6, type: 'tool.succeeded', payload: { internal_tool_name: 'brand_search', duration_ms: 1200, points: 10 } },
        { id: 7, type: 'run.completed', payload: { outcome: 'completed' } },
      ],
    });

    const { result } = renderHook(() => useRunHistoryReplay('s1', [makeRun('run-1', 'completed')], undefined));

    await waitFor(() => expect(result.current['run-1'].toolCalls).toHaveLength(1));
    expect(result.current['run-1']).toMatchObject({
      status: 'completed',
      connection: 'closed',
      hasThinking: true,
      thinking: '正在检索品牌',
      thinkingStatus: 'completed',
    });
    expect(result.current['run-1'].toolCalls[0]).toMatchObject({
      name: 'brand_search',
      status: 'succeeded',
      durationMs: 1200,
      points: 10,
    });
    expect(result.current['run-1'].steps.length).toBeGreaterThan(0);
  });

  it('does not replay the active run (it stays on the live subscription path)', async () => {
    stubReplay({});
    const runs = [makeRun('run-old', 'completed'), makeRun('run-active', 'completed')];

    const { result } = renderHook(() => useRunHistoryReplay('s1', runs, 'run-active'));

    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledWith('run-old', 0, expect.anything(), expect.anything()));
    expect(mockFetchRunEvents).toHaveBeenCalledTimes(1);
    expect(mockFetchRunEvents).not.toHaveBeenCalledWith('run-active', expect.anything(), expect.anything(), expect.anything());
    // 活动 Run 也保留种子卡（占位），但内容由 useAgentRun 实时态覆盖。
    expect(result.current['run-active']).toMatchObject({ status: 'completed' });
  });

  it('does not replay paused runs (stream would hang) but keeps the paused shell card', async () => {
    stubReplay({});
    const runs = [makeRun('run-paused', 'paused'), makeRun('run-done', 'completed')];

    const { result } = renderHook(() => useRunHistoryReplay('s1', runs, 'run-done'));

    // paused 元数据卡保留状态（执行卡据此显示「继续」按钮），但不发起回放。
    expect(mockFetchRunEvents).not.toHaveBeenCalled();
    expect(result.current['run-paused']).toMatchObject({ status: 'paused', connection: 'closed' });
    expect(result.current['run-done']).toMatchObject({ status: 'completed' });
  });

  it('degrades to the shell card when replay fails, without blocking other runs', async () => {
    mockFetchRunEvents.mockImplementation(async (runId, _lastEventId, _signal, onEvent) => {
      if (runId === 'run-broken') throw new Error('SSE_500');
      onEvent({ id: 1, runId, type: 'tool.started', payload: { internal_tool_name: 'kol_search' } });
      onEvent({ id: 2, runId, type: 'run.completed', payload: {} });
    });
    const runs = [makeRun('run-broken', 'completed'), makeRun('run-fine', 'completed')];

    const { result } = renderHook(() => useRunHistoryReplay('s1', runs, undefined));

    // 失败 Run 降级为空壳卡；其它 Run 回放不受影响。
    await waitFor(() => expect(result.current['run-fine'].toolCalls).toHaveLength(1));
    expect(result.current['run-broken']).toMatchObject({ status: 'completed', steps: [], toolCalls: [] });
  });

  it('replays each run only once across session detail refetches', async () => {
    stubReplay({
      'run-1': [{ id: 1, type: 'run.completed', payload: {} }],
    });
    const runs = [makeRun('run-1', 'completed')];

    const { rerender } = renderHook(
      ({ sessionRuns }: { sessionRuns: ApiAgentRun[] }) => useRunHistoryReplay('s1', sessionRuns, undefined),
      { initialProps: { sessionRuns: runs } },
    );
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledTimes(1));

    // settle 回拉会话详情产生新数组（同 runId）：不得重复回放。
    rerender({ sessionRuns: [makeRun('run-1', 'completed')] });
    rerender({ sessionRuns: [makeRun('run-1', 'completed')] });
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledTimes(1));
  });

  it('caps concurrent replays at HISTORY_REPLAY_CONCURRENCY', async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const release: Array<() => void> = [];
    mockFetchRunEvents.mockImplementation(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise<void>(resolve => release.push(resolve));
      inFlight -= 1;
    });
    const runCount = HISTORY_REPLAY_CONCURRENCY + 2;
    const runs = Array.from({ length: runCount }, (_, index) => makeRun(`run-${index}`, 'completed'));

    renderHook(() => useRunHistoryReplay('s1', runs, undefined));

    // 并发上限内启动，其余排队。
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledTimes(HISTORY_REPLAY_CONCURRENCY));
    expect(maxInFlight).toBeLessThanOrEqual(HISTORY_REPLAY_CONCURRENCY);

    // 放行一个后补位一个，直到全部完成。
    release.shift()?.();
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledTimes(HISTORY_REPLAY_CONCURRENCY + 1));
    release.forEach(done => done());
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledTimes(runCount));
    expect(maxInFlight).toBeLessThanOrEqual(HISTORY_REPLAY_CONCURRENCY);
  });

  it('aborts in-flight replays and resets history on session switch', async () => {
    const signals: AbortSignal[] = [];
    mockFetchRunEvents.mockImplementation(async (runId, _lastEventId, signal) => {
      signals.push(signal);
      if (runId === 'run-a1') await new Promise<void>(() => {});
    });

    const { result, rerender } = renderHook(
      ({ sessionId, sessionRuns }: { sessionId: string; sessionRuns: ApiAgentRun[] }) => (
        useRunHistoryReplay(sessionId, sessionRuns, undefined)
      ),
      { initialProps: { sessionId: 's1', sessionRuns: [makeRun('run-a1', 'completed')] } },
    );
    await waitFor(() => expect(signals).toHaveLength(1));

    rerender({ sessionId: 's2', sessionRuns: [makeRun('run-b1', 'completed')] });

    // 旧会话悬挂回放被中止，旧结果清空，新会话历史重建。
    expect(signals[0].aborted).toBe(true);
    await waitFor(() => expect(mockFetchRunEvents).toHaveBeenCalledWith('run-b1', 0, expect.anything(), expect.anything()));
    await waitFor(() => expect(result.current['run-a1']).toBeUndefined());
    expect(result.current['run-b1']).toMatchObject({ status: 'completed' });
  });
});
