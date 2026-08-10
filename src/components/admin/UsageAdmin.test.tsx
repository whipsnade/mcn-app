import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { listAdminTenants, listAdminUsage, type AdminTenant, type AdminUsage } from '../../api/adminGateway';
import UsageAdmin from './UsageAdmin';

vi.mock('../../api/adminGateway', () => ({
  listAdminTenants: vi.fn(),
  listAdminUsage: vi.fn(),
}));

const TENANT: AdminTenant = {
  id: 'tenant-a', slug: 'tenant-a', name: '租户 A', status: 'active', is_internal: false,
  runtime_backend: 'pi', license_status: 'active', active_license_id: 'lic-1',
  active_runtime_config_id: 'cfg-1', member_count: 2, active_run_count: 0,
  created_at: '2026-08-09T00:00:00Z', updated_at: '2026-08-09T00:00:00Z',
};

const USAGE_MISSING: AdminUsage = {
  tenant_id: 'tenant-a', user_id: null, run_id: null, day: '2026-08-08',
  record_count: 12, input_tokens: 1200, output_tokens: 3400, cache_read_tokens: 10,
  cache_write_tokens: 20, cost_micros: 56000, priced_cost_micros: 55000,
  usage_unavailable_count: 2, unpriced_count: 0,
};

const USAGE_UNPRICED: AdminUsage = {
  tenant_id: 'tenant-a', user_id: null, run_id: null, day: '2026-08-09',
  record_count: 3, input_tokens: null, output_tokens: null, cache_read_tokens: null,
  cache_write_tokens: null, cost_micros: null, priced_cost_micros: 0,
  usage_unavailable_count: 0, unpriced_count: 1,
};

async function selectTenant() {
  fireEvent.change(await screen.findByLabelText('选择租户'), { target: { value: 'tenant-a' } });
}

describe('UsageAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAdminTenants).mockResolvedValue({ items: [TENANT], total: 1 });
    vi.mocked(listAdminUsage).mockResolvedValue({ items: [USAGE_MISSING, USAGE_UNPRICED], limit: 20, offset: 0 });
  });

  it('prompts to pick a tenant first', () => {
    render(<UsageAdmin />);
    expect(screen.getByText('请先选择租户')).toBeTruthy();
    expect(listAdminUsage).not.toHaveBeenCalled();
  });

  it('loads usage rows with token and cost columns after picking a tenant', async () => {
    render(<UsageAdmin />);
    await selectTenant();
    expect(await screen.findByText('2026-08-08')).toBeTruthy();
    await waitFor(() => {
      expect(listAdminUsage).toHaveBeenCalledWith('tenant-a', 'day', { limit: 20, offset: 0 });
    });
    expect(screen.getByText('1200')).toBeTruthy();
    expect(screen.getByText('56000')).toBeTruthy();
    const table = screen.getByRole('table', { name: '用量明细' });
    expect(table.closest('div')?.className).toContain('overflow-x-auto');
  });

  it('flags unavailable usage and unpriced rows with textual badges', async () => {
    render(<UsageAdmin />);
    await selectTenant();
    expect(await screen.findByText('用量缺失 ×2')).toBeTruthy();
    expect(screen.getByText('未定价 ×1')).toBeTruthy();
  });

  it('reloads with a new group_by and resets pagination', async () => {
    vi.mocked(listAdminUsage).mockResolvedValue({ items: [USAGE_MISSING], limit: 20, offset: 0 });
    render(<UsageAdmin />);
    await selectTenant();
    await screen.findByText('2026-08-08');

    fireEvent.change(screen.getByLabelText('分组维度'), { target: { value: 'user' } });
    await waitFor(() => {
      expect(listAdminUsage).toHaveBeenCalledWith('tenant-a', 'user', { limit: 20, offset: 0 });
    });
  });

  it('paginates with limit and offset', async () => {
    const page = Array.from({ length: 20 }, (_, index) => ({ ...USAGE_MISSING, day: `2026-08-${String(index + 1).padStart(2, '0')}` }));
    vi.mocked(listAdminUsage).mockResolvedValue({ items: page, limit: 20, offset: 0 });
    render(<UsageAdmin />);
    await selectTenant();
    await screen.findByText('2026-08-01');

    vi.mocked(listAdminUsage).mockResolvedValue({ items: [{ ...USAGE_MISSING, day: '2026-07-31' }], limit: 20, offset: 20 });
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => {
      expect(listAdminUsage).toHaveBeenCalledWith('tenant-a', 'day', { limit: 20, offset: 20 });
    });
    expect(await screen.findByText('2026-07-31')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    await waitFor(() => {
      expect(listAdminUsage).toHaveBeenCalledWith('tenant-a', 'day', { limit: 20, offset: 0 });
    });
  });

  it('shows an empty state', async () => {
    vi.mocked(listAdminUsage).mockResolvedValue({ items: [], limit: 20, offset: 0 });
    render(<UsageAdmin />);
    await selectTenant();
    expect(await screen.findByText('暂无用量记录')).toBeTruthy();
  });

  it('shows backend error codes verbatim', async () => {
    vi.mocked(listAdminUsage).mockRejectedValue(new Error('tenant_disable_blocked'));
    render(<UsageAdmin />);
    await selectTenant();
    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_disable_blocked');
  });
});
