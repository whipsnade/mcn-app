import { request } from './client';
import type { ApiAnalysisReport, ApiTask, TaskCreateResult } from './contracts';


export interface CreateTaskInput {
  content: string;
  turn_id?: string;
}

export function createIdempotencyKey(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
  return `task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function createFallbackUuid(): string {
  const bytes = new Uint8Array(16);
  const getRandomValues = globalThis.crypto?.getRandomValues;
  if (typeof getRandomValues === 'function') {
    getRandomValues.call(globalThis.crypto, bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  // UUID v4：版本位与变体位必须符合 RFC 4122，后端按 UUID 校验 turn_id。
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function createTurnId(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
  return createFallbackUuid();
}

export function createTask(
  sessionId: string,
  input: CreateTaskInput,
  idempotencyKey = createIdempotencyKey(),
): Promise<TaskCreateResult> {
  return request<TaskCreateResult>(`/api/v1/sessions/${sessionId}/tasks`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(input),
  });
}

export function getTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}`);
}

export function cancelTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST' });
}

export function retryTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/retry`, { method: 'POST' });
}

export function retryFollowups(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/followups/retry`, { method: 'POST' });
}

export function getAnalysisReport(reportId: string): Promise<ApiAnalysisReport> {
  return request<ApiAnalysisReport>(`/api/v1/analysis-reports/${reportId}`);
}
