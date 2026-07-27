import { afterEach, describe, expect, it, vi } from 'vitest';

import type { SessionThinkingEvent } from '../types';
import { authorizedFetch } from './client';
import { streamSessionThinking } from './sessionThinking';


vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
}));

function sseResponse(source: string): Response {
  return new Response(source, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('streamSessionThinking', () => {
  afterEach(() => {
    vi.resetAllMocks();
  });

  it('maps a thinking delta and requests the authorized session stream', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue(sseResponse(
      'id: event-1\n'
      + 'event: thinking.delta\n'
      + 'data: {"operation_id":"op-1","turn_id":"turn-1","session_id":"session/1",'
      + '"purpose":"agent_loop","attempt":1,"label":"正在分析","sequence":2,"text":"分析品牌"}\n\n',
    ));
    const received: SessionThinkingEvent[] = [];
    const controller = new AbortController();

    await streamSessionThinking('session/1', controller.signal, next => received.push(next));

    expect(received).toEqual([{
      id: 'event-1',
      type: 'thinking.delta',
      sessionId: 'session/1',
      operationId: 'op-1',
      turnId: 'turn-1',
      purpose: 'agent_loop',
      attempt: 1,
      label: '正在分析',
      sequence: 2,
      text: '分析品牌',
    }]);
    expect(authorizedFetch).toHaveBeenCalledTimes(1);
    const [path, init] = vi.mocked(authorizedFetch).mock.calls[0];
    expect(path).toBe('/api/v1/sessions/session%2F1/events');
    expect(init?.signal).toBe(controller.signal);
    expect(new Headers(init?.headers).get('Accept')).toBe('text/event-stream');
  });

  it('parses JSON split across multiple SSE data lines', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue(sseResponse(
      'id: event-2\n'
      + 'event: thinking.snapshot\n'
      + 'data: {"operation_id":"op-1","turn_id":"turn-1",\n'
      + 'data: "session_id":"session-1","purpose":"brainstorm","attempt":1,\n'
      + 'data: "label":"正在理解需求","sequence":4,"text":"完整快照"}\n\n',
    ));
    const received: SessionThinkingEvent[] = [];

    await streamSessionThinking(
      'session-1',
      new AbortController().signal,
      next => received.push(next),
    );

    expect(received[0]).toMatchObject({
      type: 'thinking.snapshot',
      sessionId: 'session-1',
      sequence: 4,
      text: '完整快照',
    });
  });

  it('notifies when the HTTP stream opens even without thinking events', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue(sseResponse(': keepalive\n\n'));
    const onOpen = vi.fn();

    await streamSessionThinking(
      'session-1',
      new AbortController().signal,
      () => undefined,
      onOpen,
    );

    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it.each([
    {
      name: 'non-2xx response',
      response: new Response('failed', { status: 503 }),
      error: 'SSE_503',
    },
    {
      name: 'response without a body',
      response: new Response(null, { status: 200 }),
      error: 'SSE_200',
    },
  ])('throws a stable error for $name', async ({ response, error }) => {
    vi.mocked(authorizedFetch).mockResolvedValue(response);

    await expect(streamSessionThinking(
      'session-1',
      new AbortController().signal,
      () => undefined,
    )).rejects.toThrow(error);
  });

  it.each([
    {
      name: 'unknown event type',
      source: 'id: event-3\n'
        + 'event: thinking.unknown\n'
        + 'data: {"session_id":"session-1"}\n\n',
    },
    {
      name: 'wrong optional payload type',
      source: 'id: event-4\n'
        + 'event: thinking.delta\n'
        + 'data: {"operation_id":"op-1","turn_id":"turn-1","session_id":"session-1",'
        + '"purpose":"agent_loop","attempt":1,"label":"分析","sequence":2,"text":42}\n\n',
    },
    {
      name: 'delta without text',
      source: 'id: event-5\n'
        + 'event: thinking.delta\n'
        + 'data: {"operation_id":"op-1","turn_id":"turn-1","session_id":"session-1",'
        + '"purpose":"agent_loop","attempt":1,"label":"分析","sequence":2}\n\n',
    },
    {
      name: 'terminal event without duration and status',
      source: 'id: event-6\n'
        + 'event: thinking.completed\n'
        + 'data: {"operation_id":"op-1","turn_id":"turn-1","session_id":"session-1",'
        + '"purpose":"agent_loop","attempt":1,"label":"分析","sequence":3}\n\n',
    },
  ])('throws SSE_INVALID_EVENT for $name', async ({ source }) => {
    vi.mocked(authorizedFetch).mockResolvedValue(sseResponse(source));

    await expect(streamSessionThinking(
      'session-1',
      new AbortController().signal,
      () => undefined,
    )).rejects.toThrow('SSE_INVALID_EVENT');
  });
});
