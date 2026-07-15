import { authorizedFetch } from './client';
import type { TaskEvent } from '../state/taskEvents';


interface RawSseEvent {
  id?: string;
  event?: string;
  data: string;
}

function toTaskEvent(taskId: string, raw: RawSseEvent): TaskEvent {
  const id = Number(raw.id);
  if (!Number.isSafeInteger(id) || id < 1 || !raw.event) {
    throw new Error('SSE_INVALID_EVENT');
  }
  const payload = JSON.parse(raw.data || '{}') as Record<string, unknown>;
  return { id, taskId, type: raw.event, payload };
}

export async function parseSseStream(
  body: ReadableStream<Uint8Array>,
  onRawEvent: (event: RawSseEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let current: RawSseEvent = { data: '' };

  const dispatch = () => {
    if (current.data !== '') onRawEvent(current);
    current = { data: '' };
  };
  const consumeLine = (line: string) => {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'id') current.id = value;
    if (field === 'event') current.event = value;
    if (field === 'data') current.data = current.data ? `${current.data}\n${value}` : value;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? '';
      lines.forEach(consumeLine);
      if (done) break;
    }
    if (buffer) consumeLine(buffer);
    dispatch();
  } finally {
    reader.releaseLock();
  }
}

export async function streamTaskEvents(
  taskId: string,
  lastEventId: number,
  signal: AbortSignal,
  onEvent: (event: TaskEvent) => void,
): Promise<void> {
  const headers = new Headers({ Accept: 'text/event-stream' });
  if (lastEventId > 0) headers.set('Last-Event-ID', String(lastEventId));
  const response = await authorizedFetch(`/api/v1/tasks/${taskId}/events`, { headers, signal });
  if (!response.ok || !response.body) throw new Error(`SSE_${response.status}`);
  await parseSseStream(response.body, raw => onEvent(toTaskEvent(taskId, raw)));
}
