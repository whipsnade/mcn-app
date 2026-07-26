import type { SessionThinkingEvent, ThinkingBlock } from '../types';


export type SessionThinkingConnection =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error';

export interface SessionThinkingRuntime {
  sessionId: string;
  byTurn: Record<string, ThinkingBlock[]>;
  sequenceByOperationAttempt: Record<string, number>;
  connection: SessionThinkingConnection;
}

export function initialSessionThinking(sessionId: string): SessionThinkingRuntime {
  return {
    sessionId,
    byTurn: {},
    sequenceByOperationAttempt: {},
    connection: 'idle',
  };
}

export function reduceSessionThinking(
  state: SessionThinkingRuntime,
  event: SessionThinkingEvent,
): SessionThinkingRuntime {
  if (event.sessionId !== state.sessionId) return state;

  const operationAttemptKey = `${event.operationId}:${event.attempt}`;
  const previousSequence = state.sequenceByOperationAttempt[operationAttemptKey] ?? 0;
  if (event.sequence <= previousSequence) return state;

  const turnBlocks = state.byTurn[event.turnId] ?? [];
  const blockIndex = turnBlocks.findIndex(block => (
    block.operationId === event.operationId && block.attempt === event.attempt
  ));
  const previousBlock = blockIndex >= 0 ? turnBlocks[blockIndex] : undefined;
  const terminalStatus = event.type === 'thinking.completed'
    ? 'completed'
    : event.type === 'thinking.failed'
      ? 'interrupted'
      : undefined;
  const content = event.type === 'thinking.delta'
    ? `${previousBlock?.content ?? ''}${event.text ?? ''}`
    : event.text ?? previousBlock?.content ?? '';
  const nextBlock: ThinkingBlock = {
    operationId: event.operationId,
    turnId: event.turnId,
    purpose: event.purpose,
    attempt: event.attempt,
    label: event.label,
    content,
    status: terminalStatus ?? previousBlock?.status ?? 'running',
    taskId: event.taskId ?? previousBlock?.taskId,
    goalId: event.goalId ?? previousBlock?.goalId,
    triggerMessageId: event.triggerMessageId ?? previousBlock?.triggerMessageId,
    durationMs: event.durationMs ?? previousBlock?.durationMs,
    truncated: event.truncated ?? previousBlock?.truncated ?? false,
    errorCode: event.errorCode ?? previousBlock?.errorCode,
    sequence: event.sequence,
  };
  const nextTurnBlocks = [...turnBlocks];
  if (blockIndex >= 0) {
    nextTurnBlocks[blockIndex] = nextBlock;
  } else {
    nextTurnBlocks.push(nextBlock);
  }
  return {
    ...state,
    byTurn: {
      ...state.byTurn,
      [event.turnId]: nextTurnBlocks,
    },
    sequenceByOperationAttempt: {
      ...state.sequenceByOperationAttempt,
      [operationAttemptKey]: event.sequence,
    },
  };
}
