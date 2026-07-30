import { useEffect, useRef, useState } from 'react';

import { streamTaskEvents } from '../api/taskStream';
import {
  initialTaskRuntime,
  isTerminalTaskStatus,
  reduceTaskEvent,
  type TaskRuntimeState,
} from '../state/taskEvents';
import type { Message } from '../types';

/** 历史任务流程的回放结果：runtime 重建完成，或任务已不可见（404，不渲染卡片）。 */
export interface TaskFlowReplay {
  runtime?: TaskRuntimeState;
  missing?: boolean;
}

/**
 * 会话内每个任务一张执行流程卡：
 * - 活跃任务到终态后冻结其 runtime（新任务开始后 useTaskStream 的旧状态会消失）；
 * - 刷新/重进会话后，对消息里带 taskId 的历史任务从 0 回放持久化事件重建流程；
 * - sessionId 变化时清空并中止全部回放。
 */
export function useTaskFlows(
  sessionId: string | undefined,
  messages: readonly Message[],
  activeTaskId: string | undefined,
  activeRuntime: TaskRuntimeState | undefined,
): Record<string, TaskFlowReplay> {
  const [flows, setFlows] = useState<Record<string, TaskFlowReplay>>({});
  const replayingRef = useRef(new Set<string>());
  const abortRef = useRef(new Map<string, AbortController>());

  // 切会话：清空流程并中止进行中的回放。
  useEffect(() => {
    replayingRef.current.clear();
    abortRef.current.forEach(controller => controller.abort());
    abortRef.current.clear();
    setFlows({});
  }, [sessionId]);

  // 活跃任务终态冻结：任务切换后旧 runtime 从 useTaskStream 消失，靠此保留卡片。
  useEffect(() => {
    if (!activeTaskId || !activeRuntime || activeRuntime.taskId !== activeTaskId) return;
    if (!isTerminalTaskStatus(activeRuntime.status)) return;
    setFlows(current => current[activeTaskId]?.runtime ? current : { ...current, [activeTaskId]: { runtime: activeRuntime } });
  }, [activeTaskId, activeRuntime]);

  // 历史任务回放：仅针对消息锚点上非活跃、尚未有记录的任务，一次性回放持久化事件。
  useEffect(() => {
    if (!sessionId) return;
    const anchors = new Set(
      messages
        .filter(message => message.sender === 'user' && message.taskId)
        .map(message => message.taskId as string),
    );
    anchors.forEach(taskId => {
      if (taskId === activeTaskId) return;
      if (replayingRef.current.has(taskId)) return;
      replayingRef.current.add(taskId);
      const controller = new AbortController();
      abortRef.current.set(taskId, controller);
      let runtime = initialTaskRuntime(taskId);
      streamTaskEvents(taskId, 0, controller.signal, event => {
        runtime = reduceTaskEvent(runtime, event);
      })
        .then(() => {
          setFlows(current => ({ ...current, [taskId]: { runtime } }));
        })
        .catch(error => {
          if (controller.signal.aborted) return;
          // 404：任务已删除/不可见，标记 missing 不再重试；其余错误同样不再重试（卡片省略）。
          setFlows(current => ({
            ...current,
            [taskId]: { missing: true },
          }));
          if (!(error instanceof Error && error.message === 'SSE_404')) {
            console.warn('task flow replay failed', taskId, error);
          }
        })
        .finally(() => {
          abortRef.current.delete(taskId);
        });
    });
  }, [sessionId, messages, activeTaskId]);

  return flows;
}
