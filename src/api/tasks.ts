import { authorizedFetch, request } from './client';
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

export function retryTask(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/retry`, { method: 'POST' });
}

export function retryFollowups(taskId: string): Promise<ApiTask> {
  return request<ApiTask>(`/api/v1/tasks/${taskId}/followups/retry`, { method: 'POST' });
}

export function getCandidates(taskId: string): Promise<ApiCandidatePage> {
  return request<ApiCandidatePage>(`/api/v1/tasks/${taskId}/candidates`);
}

export function getReport(reportId: string): Promise<ApiBiReport> {
  return request<ApiBiReport>(`/api/v1/reports/${reportId}`);
}

export interface DownloadedExport {
  blob: Blob;
  filename: string;
}

export async function downloadLatestSessionExport(sessionId: string): Promise<DownloadedExport> {
  const response = await authorizedFetch(`/api/v1/sessions/${sessionId}/exports/latest.xlsx`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP_${response.status}` }));
    throw new Error(body.detail ?? `HTTP_${response.status}`);
  }
  const disposition = response.headers.get('content-disposition') ?? '';
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encodedName ? decodeURIComponent(encodedName) : 'KOL匹配度分析报告.xlsx';
  return { blob: await response.blob(), filename };
}
