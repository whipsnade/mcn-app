import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  activateAdminRuntimeConfig,
  createAdminRuntimeConfig,
  listAdminRuntimeConfigs,
  listAdminTenants,
  type AdminRuntimeConfig,
  type AdminTenant,
} from '../../api/adminGateway';
import RuntimeConfigAdmin from './RuntimeConfigAdmin';

vi.mock('../../api/adminGateway', () => ({
  listAdminTenants: vi.fn(),
  listAdminRuntimeConfigs: vi.fn(),
  createAdminRuntimeConfig: vi.fn(),
  activateAdminRuntimeConfig: vi.fn(),
}));

const TENANT: AdminTenant = {
  id: 'tenant-a', slug: 'tenant-a', name: '租户 A', status: 'active', is_internal: false,
  runtime_backend: 'pi', license_status: 'active', active_license_id: 'lic-1',
  active_runtime_config_id: 'cfg-1', member_count: 2, active_run_count: 0,
  created_at: '2026-08-09T00:00:00Z', updated_at: '2026-08-09T00:00:00Z',
};

const CONFIG_ACTIVE: AdminRuntimeConfig = {
  id: 'cfg-1', scope: 'tenant', tenant_id: 'tenant-a', version: 1, status: 'active',
  runtime_backend: 'pi', runtime_contract_version: 'marketing_runtime_v1',
  environment: 'production',
  model: { name: 'deepseek-v4-pro', provider: 'tencent', masked_origin: 'https://api***' },
  datatap: { service: 'datatap', schema_digest: 'sha256:abc' },
  limits: { max_decisions: 50 }, billing: { mcp_call_points: 10 },
  secret_refs: [{ kind: 'model_api_key', masked_value: '••••', fingerprint: 'stored' }],
  created_by: 'admin-1', created_at: '2026-08-01T00:00:00Z', activated_at: '2026-08-02T00:00:00Z',
};

const CONFIG_DRAFT: AdminRuntimeConfig = {
  ...CONFIG_ACTIVE,
  id: 'cfg-2', version: 2, status: 'draft', activated_at: null,
  created_at: '2026-08-08T00:00:00Z',
};

async function selectTenant() {
  fireEvent.change(await screen.findByLabelText('选择租户'), { target: { value: 'tenant-a' } });
}

describe('RuntimeConfigAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAdminTenants).mockResolvedValue({ items: [TENANT], total: 1 });
    vi.mocked(listAdminRuntimeConfigs).mockResolvedValue({ items: [CONFIG_ACTIVE, CONFIG_DRAFT], total: 2 });
  });

  it('prompts to pick a tenant first', () => {
    render(<RuntimeConfigAdmin />);
    expect(screen.getByText('请先选择租户')).toBeTruthy();
    expect(listAdminRuntimeConfigs).not.toHaveBeenCalled();
  });

  it('lists version history with status, backend, contract and fingerprint summary', async () => {
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    expect(await screen.findByText('v1')).toBeTruthy();
    expect(screen.getByText('v2')).toBeTruthy();
    expect(screen.getByText('生效中')).toBeTruthy();
    expect(screen.getByText('草稿')).toBeTruthy();
    expect(screen.getAllByText(/marketing_runtime_v1/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/model_api_key: stored/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/2026-08-02/).length).toBeGreaterThan(0);
    const table = screen.getByRole('table', { name: 'Runtime 配置版本历史' });
    expect(table.closest('div')?.className).toContain('overflow-x-auto');
  });

  it('shows an empty state', async () => {
    vi.mocked(listAdminRuntimeConfigs).mockResolvedValue({ items: [], total: 0 });
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    expect(await screen.findByText('暂无 Runtime 配置版本')).toBeTruthy();
  });

  it('shows backend error codes verbatim', async () => {
    vi.mocked(listAdminRuntimeConfigs).mockRejectedValue(new Error('pi_rollout_precondition_failed'));
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    expect(await screen.findByRole('alert')).toHaveTextContent('pi_rollout_precondition_failed');
  });

  it('creates a draft with write-only secrets and never echoes plaintext', async () => {
    vi.mocked(createAdminRuntimeConfig).mockResolvedValue({ ...CONFIG_DRAFT, id: 'cfg-3', version: 3 });
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    await screen.findByText('v1');

    fireEvent.click(screen.getByRole('button', { name: '新建配置版本' }));
    await screen.findByRole('dialog', { name: '新建 Runtime 配置版本' });

    fireEvent.change(screen.getByLabelText('Runtime Backend'), { target: { value: 'pi' } });
    fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'deepseek-v4-pro' } });
    fireEvent.change(screen.getByLabelText('模型提供方'), { target: { value: 'tencent' } });
    fireEvent.change(screen.getByLabelText('模型来源（脱敏）'), { target: { value: 'https://api***' } });
    fireEvent.change(screen.getByLabelText('DataTap 服务名'), { target: { value: 'datatap' } });
    fireEvent.change(screen.getByLabelText('DataTap Schema 摘要'), { target: { value: 'sha256:abc' } });
    fireEvent.change(screen.getByLabelText('决策轮次上限'), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText('MCP 调用积分'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('模型 Base URL'), { target: { value: 'https://secret.example.com' } });
    fireEvent.change(screen.getByLabelText('模型 API Key'), { target: { value: 'sk-plain-secret' } });
    fireEvent.change(screen.getByLabelText('DataTap Token'), { target: { value: 'dt-plain-token' } });
    fireEvent.change(screen.getByLabelText('DataTap 服务 URL'), { target: { value: 'https://mcp.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: '创建草稿' }));

    await waitFor(() => {
      expect(createAdminRuntimeConfig).toHaveBeenCalledWith(expect.objectContaining({
        tenant_id: 'tenant-a',
        runtime_backend: 'pi',
        model: { name: 'deepseek-v4-pro', provider: 'tencent', masked_origin: 'https://api***' },
        datatap: { service: 'datatap', schema_digest: 'sha256:abc' },
        limits: { max_decisions: 50 },
        billing: { mcp_call_points: 10 },
        secrets: {
          model_base_url: 'https://secret.example.com',
          model_api_key: 'sk-plain-secret',
          datatap_token: 'dt-plain-token',
          datatap_urls: { mcp: 'https://mcp.example.com' },
        },
      }));
    });
    // 提交后对话框关闭、列表新增版本，页面绝不回显明文 secret
    expect(await screen.findByText('v3')).toBeTruthy();
    expect(screen.queryByText('sk-plain-secret')).toBeNull();
    expect(screen.queryByText('dt-plain-token')).toBeNull();
    expect(screen.queryByDisplayValue('sk-plain-secret')).toBeNull();
  });

  it('requires all four secrets once any secret is filled', async () => {
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    await screen.findByText('v1');
    fireEvent.click(screen.getByRole('button', { name: '新建配置版本' }));
    await screen.findByRole('dialog');
    fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'm' } });
    fireEvent.change(screen.getByLabelText('模型提供方'), { target: { value: 'p' } });
    fireEvent.change(screen.getByLabelText('模型来源（脱敏）'), { target: { value: 'x' } });
    fireEvent.change(screen.getByLabelText('DataTap 服务名'), { target: { value: 's' } });
    fireEvent.change(screen.getByLabelText('DataTap Schema 摘要'), { target: { value: 'd' } });
    fireEvent.change(screen.getByLabelText('模型 API Key'), { target: { value: 'sk-only' } });
    const submit = screen.getByRole('button', { name: '创建草稿' });
    expect(submit).toBeDisabled();
    expect(screen.getByText('四个 secret 需全部填写，或全部留空')).toBeTruthy();
  });

  it('activates a draft after confirming the append-only semantics', async () => {
    vi.mocked(activateAdminRuntimeConfig).mockResolvedValue({ ...CONFIG_DRAFT, status: 'active' });
    render(<RuntimeConfigAdmin />);
    await selectTenant();
    await screen.findByText('v2');

    const draftRow = screen.getByText('v2').closest('tr') as HTMLElement;
    expect(within(draftRow).queryByRole('button', { name: '停用' })).toBeNull();
    const activeRow = screen.getByText('v1').closest('tr') as HTMLElement;
    expect(within(activeRow).queryByRole('button', { name: '激活' })).toBeNull();

    fireEvent.click(within(draftRow).getByRole('button', { name: '激活' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('append-only');
    expect(dialog.textContent).toContain('retired');
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(activateAdminRuntimeConfig).not.toHaveBeenCalled();

    fireEvent.click(within(draftRow).getByRole('button', { name: '激活' }));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '确认激活' }));
    await waitFor(() => {
      expect(activateAdminRuntimeConfig).toHaveBeenCalledWith('cfg-2');
    });
  });
});
