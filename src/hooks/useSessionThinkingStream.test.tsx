import { act, render, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { streamSessionThinking } from '../api/sessionThinking';
import type { SessionThinkingEvent } from '../types';
import { useSessionThinkingStream } from './useSessionThinkingStream';


vi.mock('../api/sessionThinking', () => ({
  streamSessionThinking: vi.fn(),
}));

function event(
  sessionId: string,
  type: SessionThinkingEvent['type'],
  payload: Partial<SessionThinkingEvent> = {},
): SessionThinkingEvent {
  return {
    id: `event-${payload.sequence ?? 1}`,
    type,
    sessionId,
    operationId: 'op-1',
    turnId: 'turn-1',
    purpose: 'agent_loop',
    attempt: 1,
    label: '正在分析数据',
    sequence: 1,
    ...payload,
  };
}

function pendingUntilAbort(signal: AbortSignal): Promise<void> {
  return new Promise((_, reject) => {
    signal.addEventListener('abort', () => {
      reject(new DOMException('aborted', 'AbortError'));
    }, { once: true });
  });
}

describe('useSessionThinkingStream', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  it('marks the first connection connected when the stream opens', async () => {
    vi.mocked(streamSessionThinking).mockImplementation(async (
      _sessionId,
      signal,
      _onEvent,
      onOpen,
    ) => {
      onOpen?.();
      await pendingUntilAbort(signal);
    });

    const { result, unmount } = renderHook(() => useSessionThinkingStream('session-1'));
    await waitFor(() => expect(streamSessionThinking).toHaveBeenCalledTimes(1));

    expect(result.current?.connection).toBe('connected');
    expect(result.current?.byTurn).toEqual({});
    unmount();
  });

  it('reconnects with exponential backoff after stream errors', async () => {
    vi.useFakeTimers();
    vi.mocked(streamSessionThinking)
      .mockRejectedValueOnce(new Error('network-1'))
      .mockRejectedValueOnce(new Error('network-2'))
      .mockImplementation(async (_sessionId, signal) => {
        await pendingUntilAbort(signal);
      });

    const { result, unmount } = renderHook(() => useSessionThinkingStream('session-1'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(streamSessionThinking).toHaveBeenCalledTimes(1);
    expect(result.current?.connection).toBe('error');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(24);
    });
    expect(streamSessionThinking).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(streamSessionThinking).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(49);
    });
    expect(streamSessionThinking).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(streamSessionThinking).toHaveBeenCalledTimes(3);
    expect(result.current?.connection).toBe('reconnecting');
    unmount();
  });

  it('removes the reconnect abort listener after a normal delay expires', async () => {
    vi.useFakeTimers();
    const addEventListener = vi.spyOn(AbortSignal.prototype, 'addEventListener');
    const removeEventListener = vi.spyOn(AbortSignal.prototype, 'removeEventListener');
    vi.mocked(streamSessionThinking)
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementation(() => new Promise<void>(() => undefined));

    const { unmount } = renderHook(() => useSessionThinkingStream('session-1'));
    await act(async () => {
      await Promise.resolve();
    });
    const reconnectAbortListener = addEventListener.mock.calls.find(
      ([type]) => type === 'abort',
    )?.[1];

    expect(reconnectAbortListener).toBeTypeOf('function');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25);
    });

    expect(removeEventListener).toHaveBeenCalledWith('abort', reconnectAbortListener);
    unmount();
  });

  it('aborts the old connection and clears runtime state when session changes', async () => {
    const connections: Array<{
      sessionId: string;
      signal: AbortSignal;
      receive: (event: SessionThinkingEvent) => void;
    }> = [];
    vi.mocked(streamSessionThinking).mockImplementation(async (sessionId, signal, onEvent) => {
      connections.push({ sessionId, signal, receive: onEvent });
      await pendingUntilAbort(signal);
    });
    const { result, rerender, unmount } = renderHook(
      ({ sessionId }) => useSessionThinkingStream(sessionId),
      { initialProps: { sessionId: 'session-1' } },
    );
    await waitFor(() => expect(connections).toHaveLength(1));
    act(() => {
      connections[0].receive(event('session-1', 'thinking.started'));
    });
    expect(result.current?.byTurn['turn-1']).toHaveLength(1);

    rerender({ sessionId: 'session-2' });

    await waitFor(() => expect(connections).toHaveLength(2));
    expect(connections[0].signal.aborted).toBe(true);
    expect(result.current).toMatchObject({
      sessionId: 'session-2',
      byTurn: {},
      connection: 'connecting',
    });

    act(() => {
      connections[0].receive(event('session-1', 'thinking.delta', {
        text: '旧连接迟到事件',
        sequence: 2,
      }));
    });
    expect(result.current?.byTurn).toEqual({});
    unmount();
  });

  it('never exposes the previous session runtime during the switch render', async () => {
    const renders: Array<ReturnType<typeof useSessionThinkingStream>> = [];
    vi.mocked(streamSessionThinking).mockImplementation(async (
      _sessionId,
      signal,
      onEvent,
      onOpen,
    ) => {
      onOpen?.();
      if (_sessionId === 'session-1') {
        onEvent(event('session-1', 'thinking.started'));
      }
      await pendingUntilAbort(signal);
    });
    function Harness({ sessionId }: { sessionId: string }) {
      renders.push(useSessionThinkingStream(sessionId));
      return null;
    }
    const mounted = render(<Harness sessionId="session-1" />);
    await waitFor(() => expect(renders.at(-1)?.byTurn['turn-1']).toHaveLength(1));
    const switchRenderIndex = renders.length;

    mounted.rerender(<Harness sessionId="session-2" />);

    expect(renders[switchRenderIndex]).toMatchObject({
      sessionId: 'session-2',
      byTurn: {},
    });
    mounted.unmount();
  });

  it('retains completed blocks in runtime after the stream reconnects', async () => {
    let receive: ((event: SessionThinkingEvent) => void) | undefined;
    let disconnect: (() => void) | undefined;
    vi.mocked(streamSessionThinking)
      .mockImplementationOnce(async (_sessionId, _signal, onEvent) => {
        receive = onEvent;
        await new Promise<void>(resolve => {
          disconnect = resolve;
        });
      })
      .mockImplementation(async (_sessionId, signal) => {
        await pendingUntilAbort(signal);
      });
    const { result, unmount } = renderHook(() => useSessionThinkingStream('session-1'));
    await waitFor(() => expect(receive).toBeDefined());

    act(() => {
      receive?.(event('session-1', 'thinking.started'));
      receive?.(event('session-1', 'thinking.delta', {
        text: '最终思考',
        sequence: 2,
      }));
      receive?.(event('session-1', 'thinking.completed', {
        durationMs: 900,
        sequence: 3,
      }));
    });
    act(() => {
      disconnect?.();
    });

    await waitFor(() => expect(streamSessionThinking).toHaveBeenCalledTimes(2));
    expect(result.current?.byTurn['turn-1'][0]).toMatchObject({
      content: '最终思考',
      status: 'completed',
      durationMs: 900,
    });
    unmount();
  });
});
