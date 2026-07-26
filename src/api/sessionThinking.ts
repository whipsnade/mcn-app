import type {
  ApiSessionThinkingEventPayload,
  ApiSessionThinkingEventType,
} from './contracts';
import type { SessionThinkingEvent } from '../types';
import { authorizedFetch } from './client';
import { parseSseStream } from './taskStream';


const THINKING_EVENT_TYPES = new Set<ApiSessionThinkingEventType>([
  'thinking.started',
  'thinking.delta',
  'thinking.snapshot',
  'thinking.completed',
  'thinking.failed',
]);

function isThinkingEventType(value: string | undefined): value is ApiSessionThinkingEventType {
  return value !== undefined && THINKING_EVENT_TYPES.has(value as ApiSessionThinkingEventType);
}

function isPayload(value: unknown): value is ApiSessionThinkingEventPayload {
  if (typeof value !== 'object' || value === null) return false;
  const payload = value as Partial<ApiSessionThinkingEventPayload>;
  const optionalNullableString = (candidate: unknown) => (
    candidate === undefined || candidate === null || typeof candidate === 'string'
  );
  const optionalString = (candidate: unknown) => (
    candidate === undefined || typeof candidate === 'string'
  );
  return (
    typeof payload.operation_id === 'string'
    && typeof payload.turn_id === 'string'
    && typeof payload.session_id === 'string'
    && typeof payload.purpose === 'string'
    && Number.isInteger(payload.attempt)
    && Number(payload.attempt) >= 1
    && typeof payload.label === 'string'
    && Number.isInteger(payload.sequence)
    && Number(payload.sequence) >= 1
    && optionalNullableString(payload.task_id)
    && optionalNullableString(payload.goal_id)
    && optionalNullableString(payload.trigger_message_id)
    && optionalString(payload.text)
    && (
      payload.status === undefined
      || payload.status === 'running'
      || payload.status === 'completed'
      || payload.status === 'interrupted'
    )
    && (
      payload.duration_ms === undefined
      || (
        Number.isFinite(payload.duration_ms)
        && Number(payload.duration_ms) >= 0
      )
    )
    && optionalNullableString(payload.error_code)
    && (payload.truncated === undefined || typeof payload.truncated === 'boolean')
  );
}

function hasEventFields(
  eventType: ApiSessionThinkingEventType,
  payload: ApiSessionThinkingEventPayload,
): boolean {
  if (eventType === 'thinking.delta' || eventType === 'thinking.snapshot') {
    return typeof payload.text === 'string';
  }
  if (eventType === 'thinking.completed') {
    return payload.status === 'completed' && payload.duration_ms !== undefined;
  }
  if (eventType === 'thinking.failed') {
    return payload.status === 'interrupted' && payload.duration_ms !== undefined;
  }
  return true;
}

function toSessionThinkingEvent(
  raw: { id?: string; event?: string; data: string },
): SessionThinkingEvent {
  if (!raw.id || !isThinkingEventType(raw.event)) {
    throw new Error('SSE_INVALID_EVENT');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.data || '{}');
  } catch {
    throw new Error('SSE_INVALID_EVENT');
  }
  if (!isPayload(parsed) || !hasEventFields(raw.event, parsed)) {
    throw new Error('SSE_INVALID_EVENT');
  }

  const event: SessionThinkingEvent = {
    id: raw.id,
    type: raw.event,
    sessionId: parsed.session_id,
    operationId: parsed.operation_id,
    turnId: parsed.turn_id,
    purpose: parsed.purpose,
    attempt: parsed.attempt,
    label: parsed.label,
    sequence: parsed.sequence,
  };
  if (parsed.task_id != null) event.taskId = parsed.task_id;
  if (parsed.goal_id != null) event.goalId = parsed.goal_id;
  if (parsed.trigger_message_id != null) event.triggerMessageId = parsed.trigger_message_id;
  if (parsed.text !== undefined) event.text = parsed.text;
  if (parsed.status !== undefined) event.status = parsed.status;
  if (parsed.duration_ms !== undefined) event.durationMs = parsed.duration_ms;
  if (parsed.error_code != null) event.errorCode = parsed.error_code;
  if (parsed.truncated !== undefined) event.truncated = parsed.truncated;
  return event;
}

export async function streamSessionThinking(
  sessionId: string,
  signal: AbortSignal,
  onEvent: (event: SessionThinkingEvent) => void,
  onOpen?: () => void,
): Promise<void> {
  const response = await authorizedFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/events`,
    { signal, headers: { Accept: 'text/event-stream' } },
  );
  if (!response.ok || !response.body) throw new Error(`SSE_${response.status}`);
  onOpen?.();
  await parseSseStream(response.body, raw => onEvent(toSessionThinkingEvent(raw)));
}
