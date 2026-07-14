import { request } from './client';
import type { ApiWallet } from './contracts';

export function getWallet(): Promise<ApiWallet> {
  return request<ApiWallet>('/api/v1/wallet');
}
