import type { ApiToken } from './contracts';

let accessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export async function refreshAccessToken(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
    })
      .then(async response => {
        if (!response.ok) {
          setAccessToken(null);
          return null;
        }
        const body = await response.json() as ApiToken;
        setAccessToken(body.access_token);
        return body.access_token;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set('Content-Type', 'application/json');
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  const response = await fetch(path, { ...init, headers, credentials: 'include' });
  if (response.status === 401 && retry && await refreshAccessToken()) {
    return request<T>(path, init, false);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP_${response.status}` }));
    throw new Error(body.detail ?? `HTTP_${response.status}`);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
