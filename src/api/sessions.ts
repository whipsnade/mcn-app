import type { Message, Session } from '../types';
import { request } from './client';
import type { ApiMessage, ApiSession, CreateSessionInput } from './contracts';


const platformNames: Record<string, string> = {
  xiaohongshu: 'Xiaohongshu',
  douyin: 'Douyin',
  bilibili: 'Bilibili',
  weibo: 'Weibo',
  wechat: 'Wechat',
};


export function toMessage(message: ApiMessage): Message {
  return {
    id: message.id,
    sender: message.role === 'assistant' ? 'ai' : message.role,
    text: message.content,
    taskId: typeof message.metadata.latest_analysis_task_id === 'string'
      ? message.metadata.latest_analysis_task_id
      : typeof message.metadata.task_id === 'string' ? message.metadata.task_id : undefined,
    brainstorm: message.metadata.brainstorm,
    timestamp: new Date(message.created_at).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    }),
  };
}


export function toSession(source: ApiSession): Session {
  const latestTask = source.latest_task;
  const report = source.latest_analysis_report;
  return {
    id: source.id,
    title: source.title,
    brand: source.brand ?? '',
    campaignName: source.campaign_name,
    status: source.status,
    platform: source.platforms.map(item => platformNames[item] ?? item).join(','),
    category: source.category ?? '',
    targetAudience: source.target_audience,
    budgetMin: source.budget_min ?? undefined,
    budgetMax: source.budget_max ?? undefined,
    summary: source.messages.find(message => message.role === 'user')?.content ?? '',
    messages: source.messages.map(toMessage),
    isStarred: source.is_starred,
    kolSelectionCount: source.kol_selection_count,
    analysis: latestTask ? {
      taskId: latestTask.id,
      status: latestTask.status,
      kind: latestTask.kind,
      // 会话级报告 task_id 为 null（KOL 圈选手动分析），同样视为归属当前会话。
      analysisReportId: report && (report.task_id === null || report.task_id === latestTask.id)
        ? report.id
        : undefined,
      followupStatus: latestTask.followup_suggestions_status ?? undefined,
      followupSuggestions: latestTask.followup_suggestions ?? [],
      followupError: latestTask.followup_error ?? undefined,
    } : undefined,
    createdAt: source.created_at,
    updatedAt: source.updated_at,
  };
}


export async function listSessions(): Promise<Session[]> {
  return (await request<ApiSession[]>('/api/v1/sessions')).map(toSession);
}


export async function getSession(id: string): Promise<Session> {
  return toSession(await request<ApiSession>(`/api/v1/sessions/${id}`));
}


export async function createSession(input: CreateSessionInput): Promise<Session> {
  return toSession(await request<ApiSession>('/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify(input),
  }));
}


export async function updateSession(
  id: string,
  changes: Record<string, unknown>,
): Promise<Session> {
  return toSession(await request<ApiSession>(`/api/v1/sessions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  }));
}


export async function deleteSession(id: string): Promise<void> {
  await request(`/api/v1/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}


export async function appendMessage(id: string, content: string): Promise<Session> {
  await request(`/api/v1/sessions/${id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
  return getSession(id);
}
