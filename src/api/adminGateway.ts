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
export const updateAdminTenant = (id: string, input: { name?: string; status?: 'active' | 'disabled' }) =>
  request<AdminTenant>(`/api/v1/admin/tenants/${encodeURIComponent(id)}`, { method: 'PATCH', headers: key(), body: JSON.stringify(input) });
export const listAdminLicenses = (tenantId: string) => request<AdminLicense[]>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license`);
export const createAdminLicense = (tenantId: string, input: Omit<AdminLicense, 'id' | 'tenant_id' | 'version' | 'active' | 'created_at'>) =>
  request<AdminLicense>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license`, { method: 'POST', headers: key(), body: JSON.stringify(input) });
export const updateAdminLicense = (tenantId: string, licenseId: string, status: 'active' | 'suspended') =>
  request<AdminLicense>(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/license/${encodeURIComponent(licenseId)}`, { method: 'PATCH', headers: key(), body: JSON.stringify({ status }) });
export const listAdminUsage = (tenantId: string, groupBy: 'tenant' | 'user' | 'run' | 'day' = 'day') =>
  request<{ items: AdminUsage[]; limit: number; offset: number }>(query(`/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/usage`, { group_by: groupBy, limit: 200 }));
export const listAdminGateways = () => request<{ items: AdminGateway[]; total: number }>('/api/v1/admin/pi-runtime/gateways');
export const updateAdminGateway = (gatewayId: string, input: { desired_capacity?: number; mode?: 'active' | 'draining' }) =>
  request<AdminGateway>(`/api/v1/admin/pi-runtime/gateways/${encodeURIComponent(gatewayId)}`, { method: 'PATCH', headers: key(), body: JSON.stringify(input) });
export const listAdminRuntimeConfigs = (tenantId: string) => request<{ items: AdminRuntimeConfig[]; total: number }>(query('/api/v1/admin/runtime-configs', { tenant_id: tenantId, limit: 200 }));
export const createAdminRuntimeConfig = (input: Record<string, unknown>) => request<AdminRuntimeConfig>('/api/v1/admin/runtime-configs', { method: 'POST', headers: key(), body: JSON.stringify(input) });
export const activateAdminRuntimeConfig = (id: string) => request<AdminRuntimeConfig>(`/api/v1/admin/runtime-configs/${encodeURIComponent(id)}/activate`, { method: 'POST', headers: key() });
export const getAdminRunDiagnostics = (id: string) => request<AdminRunDiagnostics>(`/api/v1/admin/agent-runs/${encodeURIComponent(id)}/diagnostics`);
