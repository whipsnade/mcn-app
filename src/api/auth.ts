import { refreshAccessToken, request, setAccessToken } from './client';
import type { ApiToken, ApiUser } from './contracts';

export async function requestSmsCode(phone: string) {
  return request<{ mock_code: string; expires_in: number }>('/api/v1/auth/mock/sms/code', {
    method: 'POST',
    body: JSON.stringify({ phone }),
  }, false);
}

export async function loginWithSms(phone: string, code: string): Promise<ApiUser> {
  const token = await request<ApiToken>('/api/v1/auth/mock/sms/login', {
    method: 'POST',
    body: JSON.stringify({ phone, code }),
  }, false);
  setAccessToken(token.access_token);
  return getCurrentUser();
}

export async function loginWithWechat(): Promise<ApiUser> {
  const token = await request<ApiToken>('/api/v1/auth/mock/wechat/login', {
    method: 'POST',
    body: JSON.stringify({ mock_ticket: 'mock-wechat-authorized' }),
  }, false);
  setAccessToken(token.access_token);
  return getCurrentUser();
}

export function getCurrentUser(): Promise<ApiUser> {
  return request<ApiUser>('/api/v1/users/me');
}

export async function restoreSession(): Promise<ApiUser | null> {
  if (!await refreshAccessToken()) return null;
  return getCurrentUser();
}

export async function logout(): Promise<void> {
  await request<void>('/api/v1/auth/logout', { method: 'POST' }, false).catch(() => undefined);
  setAccessToken(null);
}
