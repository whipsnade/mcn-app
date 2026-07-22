import { authorizedFetch, request } from './client';
import type { ApiAnalysisReport } from './contracts';

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

export function getKolSelection(sessionId: string): Promise<{ total: number; items: KolSelectionItem[] }> {
  return request(`/api/v1/sessions/${sessionId}/kol-selection?limit=200`);
}

export function runKolAnalysis(sessionId: string): Promise<ApiAnalysisReport> {
  return request<ApiAnalysisReport>(`/api/v1/sessions/${sessionId}/kol-analysis`, { method: 'POST' });
}

// xlsx 是二进制下载，不能走 request 的 JSON 路径（错误处理模式参照 quick.ts 的 postEvaluate）。
export async function downloadKolSelection(sessionId: string): Promise<void> {
  const response = await authorizedFetch(`/api/v1/sessions/${sessionId}/kol-selection/export`);
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
