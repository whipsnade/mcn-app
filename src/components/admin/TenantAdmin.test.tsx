import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { listAdminTenants, updateAdminTenant, type AdminTenant } from '../../api/adminGateway';
import TenantAdmin from './TenantAdmin';

vi.mock('../../api/adminGateway', () => ({
  listAdminTenants: vi.fn(),
  createAdminTenant: vi.fn(),
  updateAdminTenant: vi.fn(),
}));

const TENANT: AdminTenant = {
  id: 'tenant-a', slug: 'tenant-a', name: '租户 A', status: 'active', is_internal: false,
  runtime_backend: 'current', license_status: 'active', active_license_id: 'license-a',
  active_runtime_config_id: 'config-a', member_count: 2, active_run_count: 0,
  created_at: '2026-08-09T00:00:00Z', updated_at: '2026-08-09T00:00:00Z',
};

describe('TenantAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAdminTenants).mockResolvedValue({ items: [TENANT], total: 1 });
  });

  it('renders tenant backend and sends an explicit rollout update', async () => {
    vi.mocked(updateAdminTenant).mockResolvedValue({ ...TENANT, runtime_backend: 'pi' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<TenantAdmin />);

    expect(await screen.findByText('current')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('租户 A Backend'), { target: { value: 'pi' } });
    fireEvent.click(screen.getByRole('button', { name: '应用 Backend' }));

    await waitFor(() => {
      expect(updateAdminTenant).toHaveBeenCalledWith('tenant-a', { runtime_backend: 'pi' });
    });
    expect(await screen.findByText('pi')).toBeTruthy();
  });
});
