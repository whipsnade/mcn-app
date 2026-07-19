import { request } from './client';
import type {
  ApiAdminPointsAdjustResult,
  ApiAdminUser,
  ApiAdminUserCreateInput,
  ApiAdminUserList,
  ApiAdminUserUpdateInput,
  ApiPointsHistory,
} from './contracts';

export interface AdminUserListParams {
  keyword?: string;
  channel?: string;
  limit?: number;
  offset?: number;
}

export function listAdminUsers(params: AdminUserListParams = {}): Promise<ApiAdminUserList> {
  const search = new URLSearchParams();
  if (params.keyword) search.set('keyword', params.keyword);
  if (params.channel) search.set('channel', params.channel);
  if (params.limit !== undefined) search.set('limit', String(params.limit));
  if (params.offset !== undefined) search.set('offset', String(params.offset));
  const query = search.toString();
  return request<ApiAdminUserList>(`/api/v1/admin/users${query ? `?${query}` : ''}`);
}

export function createAdminUser(input: ApiAdminUserCreateInput): Promise<ApiAdminUser> {
  return request<ApiAdminUser>('/api/v1/admin/users', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updateAdminUser(
  id: string,
  changes: ApiAdminUserUpdateInput,
): Promise<ApiAdminUser> {
  return request<ApiAdminUser>(`/api/v1/admin/users/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });
}

export async function deleteAdminUser(id: string): Promise<void> {
  await request(`/api/v1/admin/users/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

export function adjustAdminUserPoints(
  id: string,
  delta: number,
  reason: string,
  idempotencyKey?: string,
): Promise<ApiAdminPointsAdjustResult> {
  const headers: Record<string, string> = {};
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  return request<ApiAdminPointsAdjustResult>(
    `/api/v1/admin/users/${encodeURIComponent(id)}/points`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({ delta, reason }),
    },
  );
}

export function getAdminUserPointsHistory(
  id: string,
  params: { limit?: number; offset?: number } = {},
): Promise<ApiPointsHistory> {
  const search = new URLSearchParams();
  if (params.limit !== undefined) search.set('limit', String(params.limit));
  if (params.offset !== undefined) search.set('offset', String(params.offset));
  const query = search.toString();
  return request<ApiPointsHistory>(
    `/api/v1/admin/users/${encodeURIComponent(id)}/points-history${query ? `?${query}` : ''}`,
  );
}
