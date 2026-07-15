import { request } from './client';
import type { ApiFavorite } from './contracts';


export interface CreateFavoriteInput {
  kol_id: string;
  note?: string;
  source_task_id?: string;
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

export function deleteFavorite(kolId: string): Promise<void> {
  return request<void>(`/api/v1/favorites/${kolId}`, { method: 'DELETE' });
}
