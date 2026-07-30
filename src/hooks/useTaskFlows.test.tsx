import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as taskStreamApi from '../api/taskStream';
import { initialTaskRuntime, type TaskEvent, type TaskRuntimeState } from '../state/taskEvents';
import type { Message } from '../types';
import { useTaskFlows } from './useTaskFlows';


function userMessage(id: string, taskId?: string): Message {
  return { id, sender: 'user', text: '分析一下', timestamp: '2026-07-30T10:00:00Z', taskId };
}

function terminalRuntime(taskId: string): TaskRuntimeState {
  return {
    ...initialTaskRuntime(taskId),
    status: 'completed',
    phaseLabel: '分析完成',
    connection: 'closed',
    nodes: [
      { id: 'accepted', label: '任务已受理', status: 'succeeded' },
      { id: 'terminal', label: '分析完成', status: 'succeeded' },
    ],
  };
}

describe('useTaskFlows', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('freezes the active runtime at terminal status and keeps it after the task switches', async () => {
    vi.spyOn(taskStreamApi, 'streamTaskEvents').mockResolvedValue();
    const messages = [userMessage('m1', 'task-1'), userMessage('m2', 'task-2')];
    const { result, rerender } = renderHook(
      ({ activeTaskId, activeRuntime }) => useTaskFlows('s1', messages, activeTaskId, activeRuntime),
      { initialProps: { activeTaskId: 'task-1' as string | undefined, activeRuntime: terminalRuntime('task-1') as TaskRuntimeState | undefined } },
    );

    await waitFor(() => expect(result.current['task-1']?.runtime?.status).toBe('completed'));

    rerender({ activeTaskId: 'task-2', activeRuntime: undefined });

    expect(result.current['task-1']?.runtime?.phaseLabel).toBe('分析完成');
    expect(result.current['task-1']?.runtime?.nodes).toHaveLength(2);
  });

  it('replays persisted events to rebuild flows for historical tasks', async () => {
    const replay = vi.spyOn(taskStreamApi, 'streamTaskEvents')
      .mockImplementation(async (taskId: string, _lastEventId: number, _signal: AbortSignal, onEvent: (event: TaskEvent) => void) => {
        onEvent({ id: 1, taskId, type: 'task.pending', payload: {} });
        onEvent({ id: 2, taskId, type: 'plan.ready', payload: {} });
        onEvent({ id: 3, taskId, type: 'task.completed', payload: {} });
      });
    const messages = [userMessage('m1', 'task-h')];
    const { result } = renderHook(() => useTaskFlows('s1', messages, undefined, undefined));

    await waitFor(() => expect(result.current['task-h']?.runtime?.status).toBe('completed'));

    expect(replay).toHaveBeenCalledTimes(1);
    const nodes = result.current['task-h']?.runtime?.nodes ?? [];
    expect(nodes.map(node => node.label)).toEqual(['任务已受理', '分析规划完成', '分析完成']);
    expect(result.current['task-h']?.runtime?.phaseLabel).toBe('分析完成');
  });

  it('does not replay the active task', async () => {
    const replay = vi.spyOn(taskStreamApi, 'streamTaskEvents').mockResolvedValue();
    const messages = [userMessage('m1', 'task-1')];
    renderHook(() => useTaskFlows('s1', messages, 'task-1', undefined));

    await new Promise(resolve => window.setTimeout(resolve, 20));
    expect(replay).not.toHaveBeenCalled();
  });

  it('marks replay 404 as missing and never retries', async () => {
    const replay = vi.spyOn(taskStreamApi, 'streamTaskEvents')
      .mockRejectedValue(new Error('SSE_404'));
    const { result, rerender } = renderHook(
      ({ messages }) => useTaskFlows('s1', messages, undefined, undefined),
      { initialProps: { messages: [userMessage('m1', 'task-ghost')] } },
    );

    await waitFor(() => expect(result.current['task-ghost']?.missing).toBe(true));

    rerender({ messages: [userMessage('m1', 'task-ghost'), userMessage('m2')] });
    await new Promise(resolve => window.setTimeout(resolve, 20));
    expect(replay).toHaveBeenCalledTimes(1);
  });

  it('clears flows when the session changes', async () => {
    vi.spyOn(taskStreamApi, 'streamTaskEvents').mockResolvedValue();
    const { result, rerender } = renderHook(
      ({ sessionId, messages, activeTaskId, activeRuntime }) => useTaskFlows(sessionId, messages, activeTaskId, activeRuntime),
      {
        initialProps: {
          sessionId: 's1',
          messages: [userMessage('m1', 'task-1')],
          activeTaskId: 'task-1' as string | undefined,
          activeRuntime: terminalRuntime('task-1') as TaskRuntimeState | undefined,
        },
      },
    );

    await waitFor(() => expect(result.current['task-1']?.runtime).toBeDefined());

    // 真实切会话：消息、活跃任务一并切换（任务按会话隔离，不会跨会话复用 taskId）。
    rerender({ sessionId: 's2', messages: [], activeTaskId: undefined, activeRuntime: undefined });

    await waitFor(() => expect(result.current).toEqual({}));
  });
});
