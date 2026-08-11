import { request } from './client';

export type AdminTenant = {
  id: string; slug: string; name: string; status: 'active' | 'disabled'; is_internal: boolean;
  runtime_backend: 'current' | 'pi'; license_status: 'active' | 'suspended';
  active_license_id: string | null; active_runtime_config_id: string | null;
  member_count: number; active_run_count: number; created_at: string; updated_at: string;
};
export type AdminLicense = {
  id: string; tenant_id: string; version: number; valid_from: string; valid_until: string | null;
  features: Record<string, boolean>; max_concurrent_runs: number; max_user_concurrent_runs: number;
  active: boolean; created_at: string;
};
export type AdminUsage = {
  tenant_id: string; user_id: string | null; run_id: string | null; day: string | null;
  record_count: number; input_tokens: number | null; output_tokens: number | null;
  cache_read_tokens: number | null; cache_write_tokens: number | null; cost_micros: number | null;
  priced_cost_micros: number; usage_unavailable_count: number; unpriced_count: number;
};
export type AdminGateway = {
  id: string; gateway_id: string; status: 'active' | 'offline' | 'disabled'; mode: 'active' | 'draining';
  desired_capacity: number; last_seen_at: string | null; updated_at: string;
};
export type AdminRuntimeConfig = {
  id: string; scope: 'system' | 'tenant'; tenant_id: string | null; version: number;
  status: 'draft' | 'active' | 'retired'; runtime_backend: 'current' | 'pi';
  runtime_contract_version: string; model: Record<string, unknown>; datatap: Record<string, unknown>;
  limits: Record<string, unknown>; billing: Record<string, unknown>;
  secret_refs: Array<Record<string, string>>; created_by: string | null; created_at: string; activated_at: string | null;
};
export type AdminRunDiagnostics = {
  run: Record<string, unknown>; attempts: Array<Record<string, unknown>>; steps: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>; events: Array<Record<string, unknown>>;
  usage: Array<Record<string, unknown>>; reconciliation: Record<string, unknown> | null;
};
export type AdminTenantUser = {
  id: string; nickname: string; role: 'owner' | 'admin' | 'member';
  status: 'active' | 'disabled'; created_at: string;
};
export type AdminWalletAdjustResult = {
  tenant_id: string; balance: number; reserved: number; transaction_id: string;
};
export type AdminWalletState = {
  tenant_id: string; balance: number; reserved: number;
};
export type AdminQuotaItem = {
  user_id: string; period: 'monthly'; points_limit: number; status: 'active' | 'disabled';
};

const key = (value?: string): Record<string, string> => ({ 'Idempotency-Key': value ?? crypto.randomUUID() });
const query = (path: string, params: Record<string, string | number | undefined>): string => {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([name, value]) => { if (value !== undefined) search.set(name, String(value)); });
  const encoded = search.toString();
  return encoded ? `${path}?${encoded}` : path;
};

export const listAdminTenants = (params: { limit?: number; offset?: number } = {}) =>
  request<{ items: AdminTenant[]; total: number }>(query('/api/v1/admin/tenants', params));
export const createAdminTenant = (input: { slug: string; name: string; is_internal?: boolean }) =>
  request<AdminTenant>('/api/v1/admin/tenants', { method: 'POST', headers: key(), body: JSON.stringify(input) });
export const updateAdminTenant = (id: string, input: { name?: string; status?: 'active' | 'disabled'; runtime_backend?: 'current' | 'pi' }) =>
  request<AdminTenant>(`/api/v1/admin/tenants/${encodeURIComponent(id)}`, { method: 'PATCH', headers: key(), body: JSON.stringify(input) });
export const listAdminLicenses = (tenantId: string) => request<AdminLicense[]>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license`);
// 后端 valid_from/valid_until 均可选（缺省 valid_from 取当前时间），类型在此如实放宽；运行时行为不变。
export const createAdminLicense = (tenantId: string, input: { valid_from?: string; valid_until?: string | null; features: Record<string, boolean>; max_concurrent_runs: number; max_user_concurrent_runs: number }) =>
  request<AdminLicense>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license`, { method: 'POST', headers: key(), body: JSON.stringify(input) });
export const updateAdminLicense = (tenantId: string, licenseId: string, status: 'active' | 'suspended') =>
  request<AdminLicense>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license/${encodeURIComponent(licenseId)}`, { method: 'PATCH', headers: key(), body: JSON.stringify({ status }) });
export const listAdminUsage = (tenantId: string, groupBy: 'tenant' | 'user' | 'run' | 'day' = 'day', options: { limit?: number; offset?: number } = {}) =>
  request<{ items: AdminUsage[]; limit: number; offset: number }>(query(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/usage`, { group_by: groupBy, limit: options.limit ?? 200, offset: options.offset }));
export const listAdminGateways = () => request<{ items: AdminGateway[]; total: number }>('/api/v1/admin/pi-runtime/gateways');
export const updateAdminGateway = (gatewayId: string, input: { desired_capacity?: number; mode?: 'active' | 'draining' }) =>
  request<AdminGateway>(`/api/v1/admin/pi-runtime/gateways/${encodeURIComponent(gatewayId)}`, { method: 'PATCH', headers: key(), body: JSON.stringify(input) });
export const listAdminRuntimeConfigs = (tenantId: string) => request<{ items: AdminRuntimeConfig[]; total: number }>(query('/api/v1/admin/runtime-configs', { tenant_id: tenantId, limit: 200 }));
export const createAdminRuntimeConfig = (input: Record<string, unknown>) => request<AdminRuntimeConfig>('/api/v1/admin/runtime-configs', { method: 'POST', headers: key(), body: JSON.stringify(input) });
export const activateAdminRuntimeConfig = (id: string) => request<AdminRuntimeConfig>(`/api/v1/admin/runtime-configs/${encodeURIComponent(id)}/activate`, { method: 'POST', headers: key() });
export const getAdminRunDiagnostics = (id: string) => request<AdminRunDiagnostics>(`/api/v1/admin/agent-runs/${encodeURIComponent(id)}/diagnostics`);
// 租户成员列表：钱包调整与周期额度编辑的成员选择数据源。
export const listAdminTenantUsers = (tenantId: string, options: { limit?: number; offset?: number } = {}) =>
  request<{ items: AdminTenantUser[]; total: number; limit: number; offset: number }>(query(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/users`, { limit: options.limit ?? 200, offset: options.offset }));
// 租户钱包只读投影：无钱包行时 404 tenant_wallet_not_found。
export const getTenantWallet = (tenantId: string) =>
  request<AdminWalletState>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/wallet`);
// 租户钱包人工调整：delta 非零整数，正加负减；服务端强制持久化幂等键。
export const adjustTenantWallet = (tenantId: string, input: { user_id: string; delta: number; reason: string }, idempotencyKey?: string) =>
  request<AdminWalletAdjustResult>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/wallet/adjust`, { method: 'POST', headers: key(idempotencyKey), body: JSON.stringify(input) });
export const listTenantQuota = (tenantId: string) =>
  request<{ items: AdminQuotaItem[] }>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/quota`);
// 用户周期额度 upsert：只影响新周期/新 Run 的扣费上限。
export const setTenantQuota = (tenantId: string, userId: string, input: { points_limit: number }, idempotencyKey?: string) =>
  request<AdminQuotaItem>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/quota/${encodeURIComponent(userId)}`, { method: 'PUT', headers: key(idempotencyKey), body: JSON.stringify(input) });
