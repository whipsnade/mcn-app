import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { RunEvent } from '../state/agentEvents';
import * as agentApi from '../api/agent';
import { installFetchSse } from '../test/fakeSse';
import { useAgentRun } from './useAgentRun';

describe('useAgentRun', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds run card state from run SSE events', async () => {
    const fake = installFetchSse();
    const { result } = renderHook(() => useAgentRun('run-1'));
    await fake.waitForConnection();

    fake.emit({ id: 1, type: 'run.started', payload: { run_kind: 'user' } });
    fake.emit({ id: 2, type: 'thinking.started', payload: { attempt: 1 } });
    fake.emit({ id: 3, type: 'thinking.delta', payload: { text: '正在检索' } });
    fake.emit({ id: 4, type: 'tool.started', payload: { internal_tool_name: 'brand_search' } });
    fake.emit({ id: 5, type: 'tool.succeeded', payload: { internal_tool_name: 'brand_search' } });

    await waitFor(() => {
      expect(result.current?.status).toBe('running');
      expect(result.current?.connection).toBe('connected');
    });
    expect(result.current?.thinking).toBe('正在检索');
    expect(result.current?.hasThinking).toBe(true);
    expect(result.current?.toolCalls).toEqual([
      { id: expect.any(String), name: 'brand_search', status: 'succeeded' },
    ]);
    fake.restore();
  });

  it('reconnects from the last event id without duplicating state', async () => {
    const fake = installFetchSse();
    const { unmount } = renderHook(() => useAgentRun('run-1'));
    await fake.waitForConnection();

    fake.emit({ id: 17, type: 'tool.started', payload: { internal_tool_name: 'brand_search' } });
    fake.disconnect();
    await fake.waitForReconnect();

    expect(fake.lastRequestHeaders().get('Last-Event-ID')).toBe('17');
    unmount();
    fake.restore();
  });

  it('stops streaming when the run reaches a terminal state', async () => {
    const fake = installFetchSse();
    renderHook(() => useAgentRun('run-1'));
    await fake.waitForConnection();

    fake.emit({ id: 1, type: 'run.completed', payload: { outcome: 'completed' } });
    fake.disconnect();

    await waitFor(() => expect(fake.connectionCount()).toBe(1));
    fake.restore();
  });

  it('aborts the active fetch on unmount', async () => {
    const fake = installFetchSse();
    const { unmount } = renderHook(() => useAgentRun('run-1'));
    await fake.waitForConnection();

    const signal = fake.lastRequestSignal();
    unmount();

    expect(signal.aborted).toBe(true);
    fake.restore();
  });

  it('drops stale late responses from a previous run via the generation token', async () => {
    const pending = new Promise<void>(() => {});
    const callbacks: Array<(event: RunEvent) => void> = [];
    const fetchRunEvents = vi.spyOn(agentApi, 'fetchRunEvents').mockImplementation(
      (_runId, _lastEventId, _signal, onEvent) => {
        callbacks.push(onEvent);
        return pending;
      },
    );

    const { result, rerender } = renderHook(
      ({ runId }: { runId: string | undefined }) => useAgentRun(runId),
      { initialProps: { runId: 'run-1' } },
    );
    await waitFor(() => expect(callbacks).toHaveLength(1));

    act(() => {
      callbacks[0]({ id: 1, runId: 'run-1', type: 'tool.started', payload: { internal_tool_name: 'brand_search' } });
    });
    await waitFor(() => expect(result.current?.toolCalls).toHaveLength(1));

    // switch to a new run for the (new) active session
    rerender({ runId: 'run-2' });
    await waitFor(() => {
      expect(result.current?.runId).toBe('run-2');
      expect(callbacks).toHaveLength(2);
    });

    // a late event from the previous run's connection must not pollute run-2
    act(() => {
      callbacks[0]({ id: 2, runId: 'run-1', type: 'tool.started', payload: { internal_tool_name: 'stale_tool' } });
    });
    expect(result.current?.toolCalls).toHaveLength(0);

    // the new run's own events still apply
    act(() => {
      callbacks[1]({ id: 1, runId: 'run-2', type: 'thinking.delta', payload: { text: '新思考' } });
    });
    expect(result.current?.thinking).toBe('新思考');

    expect(fetchRunEvents).toHaveBeenCalledTimes(2);
  });
});
