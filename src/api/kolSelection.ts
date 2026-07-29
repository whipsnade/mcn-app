import { authorizedFetch, request } from './client';
import type { ApiAnalysisReport, ApiSelectionSetItem } from './contracts';

export interface KolSelectionItem {
  platform: string;
  kol_uid: string;
  nickname: string;
  followers: number | null;
  city: string | null;
  profile_url: string | null;
  fields: Record<string, unknown>;
  score: Record<string, unknown>;
}

export interface KolTop10TrendItem {
  rank: number;
  platform: string;
  kol_uid: string;
  nickname: string;
  ranking_interaction: number;
  scope_status: Record<string, string>;
  trend_points: Array<{ week_start: string; average_interactions: number; post_count?: number }>;
}

export interface KolSelectionDetail {
  set_id: string;
  platform: string;
  kol_uid: string;
  detail: Record<string, unknown>;
  posts: Array<Record<string, unknown>>;
  source: 'cache' | 'query' | 'refresh' | 'missing';
  points_cost: number;
  posts_degraded: boolean;
  fetched_at: string | null;
}

export interface KolSelectionDetailQuery {
  set_id?: string;
  platform: string;
  kol_uid: string;
  refresh: boolean;
}

export function getKolSelection(
  sessionId: string,
  setId?: string,
): Promise<{ total: number; items: KolSelectionItem[] }> {
  const setQuery = setId ? `&set_id=${encodeURIComponent(setId)}` : '';
  return request(`/api/v1/sessions/${sessionId}/kol-selection?limit=200${setQuery}`);
}

export function listSelectionSets(sessionId: string): Promise<ApiSelectionSetItem[]> {
  return request<ApiSelectionSetItem[]>(`/api/v1/sessions/${sessionId}/selection-sets`);
}

export function getKolTop10Trend(sessionId: string, setId?: string): Promise<{ set_id: string | null; items: KolTop10TrendItem[] }> {
  const query = setId ? `?set_id=${encodeURIComponent(setId)}` : '';
  return request(`/api/v1/sessions/${sessionId}/kol-top10-trend${query}`);
}

export function getKolSelectionDetail(
  sessionId: string,
  query: Omit<KolSelectionDetailQuery, 'refresh'>,
): Promise<KolSelectionDetail> {
  const params = new URLSearchParams({ platform: query.platform, kol_uid: query.kol_uid });
  if (query.set_id) params.set('set_id', query.set_id);
  return request(`/api/v1/sessions/${sessionId}/kol-selection/detail?${params.toString()}`);
}

export function queryKolSelectionDetail(
  sessionId: string,
  payload: KolSelectionDetailQuery,
): Promise<KolSelectionDetail> {
  return request(`/api/v1/sessions/${sessionId}/kol-selection/detail/query`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function runKolAnalysis(sessionId: string): Promise<ApiAnalysisReport> {
  return request<ApiAnalysisReport>(`/api/v1/sessions/${sessionId}/kol-analysis`, { method: 'POST' });
}

// xlsx 是二进制下载，不能走 request 的 JSON 路径（错误处理模式参照 quick.ts 的 postEvaluate）。
export async function downloadKolSelection(sessionId: string, setId?: string): Promise<void> {
  const setQuery = setId ? `?set_id=${encodeURIComponent(setId)}` : '';
  const response = await authorizedFetch(`/api/v1/sessions/${sessionId}/kol-selection/export${setQuery}`);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `HTTP_${response.status}`);
  }
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : 'KOL匹配度分析.xlsx';
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  // Safari/Firefox 对未挂载的 <a> 及同步 revoke 会取消下载，挂到 DOM 并延迟回收。
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
