import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAdminLicense,
  listAdminLicenses,
  listAdminTenants,
  updateAdminLicense,
  type AdminLicense,
  type AdminTenant,
} from '../../api/adminGateway';
import LicenseAdmin from './LicenseAdmin';

vi.mock('../../api/adminGateway', () => ({
  listAdminTenants: vi.fn(),
  listAdminLicenses: vi.fn(),
  createAdminLicense: vi.fn(),
  updateAdminLicense: vi.fn(),
}));

const TENANT: AdminTenant = {
  id: 'tenant-a', slug: 'tenant-a', name: '租户 A', status: 'active', is_internal: false,
  runtime_backend: 'current', license_status: 'active', active_license_id: 'lic-2',
  active_runtime_config_id: null, member_count: 2, active_run_count: 0,
  created_at: '2026-08-09T00:00:00Z', updated_at: '2026-08-09T00:00:00Z',
};

const LICENSE_V1: AdminLicense = {
  id: 'lic-1', tenant_id: 'tenant-a', version: 1,
  valid_from: '2026-01-01T00:00:00Z', valid_until: '2026-12-31T00:00:00Z',
  features: { kol_selection: true, brand_analysis: false },
  max_concurrent_runs: 5, max_user_concurrent_runs: 2, active: false,
  created_at: '2026-08-01T00:00:00Z',
};

const LICENSE_V2: AdminLicense = {
  id: 'lic-2', tenant_id: 'tenant-a', version: 2,
  valid_from: '2026-08-01T00:00:00Z', valid_until: null,
  features: { kol_selection: true, brand_analysis: true },
  max_concurrent_runs: 10, max_user_concurrent_runs: 3, active: true,
  created_at: '2026-08-08T00:00:00Z',
};

async function selectTenant() {
  fireEvent.change(await screen.findByLabelText('选择租户'), { target: { value: 'tenant-a' } });
}

describe('LicenseAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAdminTenants).mockResolvedValue({ items: [TENANT], total: 1 });
    vi.mocked(listAdminLicenses).mockResolvedValue([LICENSE_V1, LICENSE_V2]);
  });

  it('prompts to pick a tenant before loading licenses', () => {
    render(<LicenseAdmin />);
    expect(screen.getByText('请先选择租户')).toBeTruthy();
    expect(listAdminLicenses).not.toHaveBeenCalled();
  });

  it('lists license versions with validity, features, concurrency and active badge', async () => {
    render(<LicenseAdmin />);
    await selectTenant();
    expect(await screen.findByText('v2')).toBeTruthy();
    expect(screen.getByText('v1')).toBeTruthy();
    expect(screen.getByText('当前生效')).toBeTruthy();
    expect(screen.getByText('历史版本')).toBeTruthy();
    expect(screen.getByText(/brand_analysis/)).toBeTruthy();
    expect(screen.getByText(/10 \/ 3/)).toBeTruthy();
    expect(screen.getByText('永久有效')).toBeTruthy();
  });

  it('renders the table inside a horizontally scrollable container', async () => {
    render(<LicenseAdmin />);
    await selectTenant();
    await screen.findByText('v2');
    const table = screen.getByRole('table', { name: 'License 版本列表' });
    expect(table.closest('div')?.className).toContain('overflow-x-auto');
  });

  it('shows an empty state when the tenant has no license versions', async () => {
    vi.mocked(listAdminLicenses).mockResolvedValue([]);
    render(<LicenseAdmin />);
    await selectTenant();
    expect(await screen.findByText('暂无 License 版本')).toBeTruthy();
  });

  it('shows backend error codes verbatim', async () => {
    vi.mocked(listAdminLicenses).mockRejectedValue(new Error('tenant_disable_blocked'));
    render(<LicenseAdmin />);
    await selectTenant();
    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_disable_blocked');
  });

  it('appends a new license version through the form dialog', async () => {
    const created: AdminLicense = { ...LICENSE_V1, id: 'lic-3', version: 3, active: false };
    vi.mocked(createAdminLicense).mockResolvedValue(created);
    render(<LicenseAdmin />);
    await selectTenant();
    await screen.findByText('v2');

    fireEvent.click(screen.getByRole('button', { name: '追加新版本' }));
    const dialog = await screen.findByRole('dialog', { name: '追加 License 版本' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');

    fireEvent.click(screen.getByLabelText('kol_selection'));
    fireEvent.click(screen.getByLabelText('brand_analysis'));
    fireEvent.change(screen.getByLabelText('租户并发上限'), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('单用户并发上限'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: '创建版本' }));

    await waitFor(() => {
      expect(createAdminLicense).toHaveBeenCalledWith('tenant-a', expect.objectContaining({
        features: { kol_selection: true, brand_analysis: true },
        max_concurrent_runs: 8,
        max_user_concurrent_runs: 2,
      }));
    });
    expect(await screen.findByText('v3')).toBeTruthy();
  });

  it('activates a version after confirming the new-run-only semantics', async () => {
    vi.mocked(updateAdminLicense).mockResolvedValue({ ...LICENSE_V1, active: true });
    render(<LicenseAdmin />);
    await selectTenant();
    await screen.findByText('v2');

    const row = screen.getByText('v1').closest('tr');
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: '激活' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('后续授权判定');
    fireEvent.click(screen.getByRole('button', { name: '确认激活' }));

    await waitFor(() => {
      expect(updateAdminLicense).toHaveBeenCalledWith('tenant-a', 'lic-1', 'active');
    });
  });

  it('suspends the active version after confirmation and can be cancelled', async () => {
    vi.mocked(updateAdminLicense).mockResolvedValue({ ...LICENSE_V2, active: false });
    render(<LicenseAdmin />);
    await selectTenant();
    await screen.findByText('v2');

    const row = screen.getByText('v2').closest('tr');
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: '暂停' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('后续授权判定');
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(updateAdminLicense).not.toHaveBeenCalled();

    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: '暂停' }));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }));
    await waitFor(() => {
      expect(updateAdminLicense).toHaveBeenCalledWith('tenant-a', 'lic-2', 'suspended');
    });
  });
});
