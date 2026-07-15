import { request } from './client';
import type { ApiBiReport, ApiCandidatePage, ApiTask } from './contracts';


export interface CreateTaskInput {
  content: string;
  scoring_profile?: 'balanced' | 'audience_first' | 'performance_first' | 'budget_first' | 'risk_first';
}

export function createTask(sessionId: string, input: CreateTaskInput): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/sessions/${sessionId}/tasks`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function getTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}`);
}

export function cancelTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST' });
}

export function getCandidates(taskId: string): Promise<ApiCandidatePage> {
  return request<ApiCandidatePage>(`/api/v1/tasks/${taskId}/candidates`);
}

export function getReport(reportId: string): Promise<ApiBiReport> {
  return request<ApiBiReport>(`/api/v1/reports/${reportId}`);
}
