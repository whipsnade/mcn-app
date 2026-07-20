import { authorizedFetch, request } from './client';
import type {
  ApiQuickEvaluateResult,
  ApiQuickKolDetail,
  ApiQuickKolRecommendations,
  ApiQuickPlatform,
  ApiQuickTopPosts,
} from './contracts';

export interface KolRecommendationsParams {
  budget: number;
  platforms?: string[];
}

export interface KolDetailParams {
  platform: string;
  kw_uid: string;
  nickname: string;
}

export function getKolRecommendations(
  params: KolRecommendationsParams,
): Promise<ApiQuickKolRecommendations> {
  const search = new URLSearchParams();
  search.set('budget', String(params.budget));
  if (params.platforms && params.platforms.length > 0) {
    search.set('platforms', params.platforms.join(','));
  }
  return request<ApiQuickKolRecommendations>(`/api/v1/quick/kol-recommendations?${search.toString()}`);
}

export function getKolDetail(params: KolDetailParams): Promise<ApiQuickKolDetail> {
  const search = new URLSearchParams();
  search.set('platform', params.platform);
  search.set('kw_uid', params.kw_uid);
  search.set('nickname', params.nickname);
  return request<ApiQuickKolDetail>(`/api/v1/quick/kol-detail?${search.toString()}`);
}

export function getTopPosts(platform: ApiQuickPlatform): Promise<ApiQuickTopPosts> {
  const search = new URLSearchParams();
  search.set('platform', platform);
  return request<ApiQuickTopPosts>(`/api/v1/quick/top-posts?${search.toString()}`);
}

export async function postEvaluate(file: File): Promise<ApiQuickEvaluateResult> {
  // client.ts 的 request 会强制 JSON Content-Type，multipart 需自行组装（鉴权/401 刷新走 authorizedFetch）。
  const form = new FormData();
  form.append('file', file);
  const response = await authorizedFetch('/api/v1/quick/evaluate', {
    method: 'POST',
    body: form,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP_${response.status}` }));
    throw new Error(body.detail ?? `HTTP_${response.status}`);
  }
  return response.json() as Promise<ApiQuickEvaluateResult>;
}

// 快捷功能错误统一兜底：积分不足提示充值，其余提示稍后重试。
export function quickErrorMessage(error: unknown, fallback = '查询失败，请稍后重试'): string {
  if (error instanceof Error) {
    if (error.message === 'INSUFFICIENT_POINTS') return '积分不足，请充值';
    if (error.message === 'QUICK_CALL_FAILED') return fallback;
  }
  return fallback;
}
