import { describe, expect, it } from 'vitest';

import type { SessionThinkingEvent } from '../types';
import {
  initialSessionThinking,
  reduceSessionThinking,
} from './sessionThinking';


function event(
  type: SessionThinkingEvent['type'],
  payload: Partial<SessionThinkingEvent> = {},
): SessionThinkingEvent {
  return {
    id: `event-${payload.sequence ?? 1}`,
    type,
    sessionId: 'session-1',
    operationId: 'op-1',
    turnId: 'turn-1',
    purpose: 'agent_loop',
    attempt: 1,
    label: '正在分析数据',
    sequence: 1,
    ...payload,
  };
}

describe('reduceSessionThinking', () => {
  it('merges started, delta, snapshot and completed by operation plus attempt', () => {
    let state = initialSessionThinking('session-1');
    state = reduceSessionThinking(state, event('thinking.started'));
    state = reduceSessionThinking(state, event('thinking.delta', {
      text: '分析品牌',
      sequence: 2,
    }));
    state = reduceSessionThinking(state, event('thinking.snapshot', {
      text: '分析品牌和平台',
      sequence: 3,
    }));
    state = reduceSessionThinking(state, event('thinking.completed', {
      durationMs: 21808,
      sequence: 4,
    }));

    expect(state.byTurn['turn-1'][0]).toMatchObject({
      operationId: 'op-1',
      attempt: 1,
      content: '分析品牌和平台',
      status: 'completed',
      durationMs: 21808,
    });
  });

  it('marks a failed operation interrupted without dropping its content', () => {
    let state = initialSessionThinking('session-1');
    state = reduceSessionThinking(state, event('thinking.started'));
    state = reduceSessionThinking(state, event('thinking.delta', {
      text: '已获得部分分析',
      sequence: 2,
    }));
    state = reduceSessionThinking(state, event('thinking.failed', {
      errorCode: 'MODEL_STREAM_INTERRUPTED',
      durationMs: 1200,
      sequence: 3,
    }));

    expect(state.byTurn['turn-1'][0]).toMatchObject({
      content: '已获得部分分析',
      status: 'interrupted',
      errorCode: 'MODEL_STREAM_INTERRUPTED',
      durationMs: 1200,
    });
  });

  it('keeps retries as separate blocks for the same operation', () => {
    let state = initialSessionThinking('session-1');
    state = reduceSessionThinking(state, event('thinking.started'));
    state = reduceSessionThinking(state, event('thinking.failed', { sequence: 2 }));
    state = reduceSessionThinking(state, event('thinking.started', {
      attempt: 2,
      sequence: 1,
    }));
    state = reduceSessionThinking(state, event('thinking.delta', {
      attempt: 2,
      text: '修复后的分析',
      sequence: 2,
    }));

    expect(state.byTurn['turn-1']).toHaveLength(2);
    expect(state.byTurn['turn-1'].map(block => [block.attempt, block.status])).toEqual([
      [1, 'interrupted'],
      [2, 'running'],
    ]);
  });

  it('ignores events belonging to another session', () => {
    const state = initialSessionThinking('session-1');
    const next = reduceSessionThinking(state, event('thinking.started', {
      sessionId: 'session-2',
    }));

    expect(next).toBe(state);
  });

  it('ignores duplicate snapshots and older operation sequences', () => {
    let state = initialSessionThinking('session-1');
    state = reduceSessionThinking(state, event('thinking.started'));
    state = reduceSessionThinking(state, event('thinking.snapshot', {
      text: '最新快照',
      sequence: 4,
    }));
    const afterSnapshot = state;

    state = reduceSessionThinking(state, event('thinking.snapshot', {
      text: '重复快照不应覆盖',
      sequence: 4,
    }));
    state = reduceSessionThinking(state, event('thinking.delta', {
      text: '旧增量不应追加',
      sequence: 3,
    }));

    expect(state).toBe(afterSnapshot);
    expect(state.byTurn['turn-1'][0].content).toBe('最新快照');
  });
});
