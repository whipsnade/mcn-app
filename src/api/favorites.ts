import { request } from './client';
import type { ApiFavorite } from './contracts';


export interface CreateFavoriteInput {
  kol_id: string;
  note?: string;
  source_task_id?: string;
}

export interface CreateFavoriteByKeyInput {
  platform: string;
  kolUid: string;
  nickname?: string;
  snapshot?: Record<string, unknown>;
}

export function listFavorites(): Promise<ApiFavorite[]> {
  return request<ApiFavorite[]>('/api/v1/favorites');
}

export function createFavorite(input: CreateFavoriteInput): Promise<ApiFavorite> {
  return request<ApiFavorite>('/api/v1/favorites', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

// 新路径：platform + kol_uid 标识收藏（圈选/推荐达人卡片），与旧 kol_id 路径并存。
export function createFavoriteByKey(input: CreateFavoriteByKeyInput): Promise<ApiFavorite> {
  return request<ApiFavorite>('/api/v1/favorites', {
    method: 'POST',
    body: JSON.stringify({
      platform: input.platform,
      kol_uid: input.kolUid,
      nickname: input.nickname,
      snapshot: input.snapshot,
    }),
  });
}

export function deleteFavoriteByKey(platform: string, kolUid: string): Promise<void> {
  const query = `platform=${encodeURIComponent(platform)}&kol_uid=${encodeURIComponent(kolUid)}`;
  return request<void>(`/api/v1/favorites?${query}`, { method: 'DELETE' });
}

export function deleteFavorite(kolId: string): Promise<void> {
  return request<void>(`/api/v1/favorites/${kolId}`, { method: 'DELETE' });
}
