import { request } from './client';
import type { ApiAnalysisReport, ApiTask } from './contracts';


export interface CreateTaskInput {
  content: string;
}

export function createIdempotencyKey(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
  return `task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function createTask(
  sessionId: string,
  input: CreateTaskInput,
  idempotencyKey = createIdempotencyKey(),
): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/sessions/${sessionId}/tasks`, {
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
