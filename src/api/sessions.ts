import type { Session } from '../types';
import { request } from './client';
import type { ApiSession, CreateSessionInput } from './contracts';


const platformNames: Record<string, string> = {
  xiaohongshu: 'Xiaohongshu',
  douyin: 'Douyin',
  bilibili: 'Bilibili',
  weibo: 'Weibo',
  wechat: 'Wechat',
};


export function toSession(source: ApiSession): Session {
  return {
    id: source.id,
    title: source.title,
    brand: source.brand,
    campaignName: source.campaign_name,
    status: source.status,
    platform: source.platforms.map(item => platformNames[item] ?? item).join(','),
    category: source.category,
    targetAudience: source.target_audience,
    budgetMin: source.budget_min ?? undefined,
    budgetMax: source.budget_max ?? undefined,
    summary: source.messages.find(message => message.role === 'user')?.content ?? '',
    messages: source.messages.map(message => ({
      id: message.id,
      sender: message.role === 'assistant' ? 'ai' : message.role,
      text: message.content,
      timestamp: new Date(message.created_at).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
      }),
    })),
    isStarred: source.is_starred,
    analysis: source.latest_task ? {
      taskId: source.latest_task.id,
      status: source.latest_task.status,
      candidateVersion: source.latest_candidates?.version,
      reportId: source.latest_report?.id,
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


export async function appendMessage(id: string, content: string): Promise<Session> {
  await request(`/api/v1/sessions/${id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
  return getSession(id);
}
