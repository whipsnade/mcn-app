import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createAdminTenant, listAdminTenants, updateAdminTenant, type AdminTenant } from '../../api/adminGateway';
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
    render(<TenantAdmin />);

    expect(await screen.findByText('current')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('租户 A Backend'), { target: { value: 'pi' } });
    fireEvent.click(screen.getByRole('button', { name: '应用 Backend' }));

    // 切换 Backend 必须先经过确认对话框，文案说明只影响新 Run
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('只影响新 Run');
    expect(dialog.textContent).toContain('在途 Run 与历史 snapshot 不变');
    fireEvent.click(screen.getByRole('button', { name: '确认切换' }));

    await waitFor(() => {
      expect(updateAdminTenant).toHaveBeenCalledWith('tenant-a', { runtime_backend: 'pi' });
    });
    expect(await screen.findByText('pi')).toBeTruthy();
  });

  it('does not call the API when the backend switch is cancelled', async () => {
    render(<TenantAdmin />);
    expect(await screen.findByText('current')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('租户 A Backend'), { target: { value: 'pi' } });
    fireEvent.click(screen.getByRole('button', { name: '应用 Backend' }));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(updateAdminTenant).not.toHaveBeenCalled();
  });

  it('creates a tenant through the inline form dialog without window.prompt', async () => {
    const promptSpy = vi.spyOn(window, 'prompt');
    const confirmSpy = vi.spyOn(window, 'confirm');
    const created: AdminTenant = { ...TENANT, id: 'tenant-b', slug: 'tenant-b', name: '租户 B', is_internal: true };
    vi.mocked(createAdminTenant).mockResolvedValue(created);
    render(<TenantAdmin />);
    expect(await screen.findByText('租户 A')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '新建租户' }));
    const dialog = await screen.findByRole('dialog', { name: '新建租户' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');

    fireEvent.change(screen.getByLabelText('租户名称'), { target: { value: '租户 B' } });
    fireEvent.change(screen.getByLabelText('租户 slug'), { target: { value: 'tenant-b' } });
    fireEvent.click(screen.getByLabelText('内部租户'));
    fireEvent.click(screen.getByRole('button', { name: '创建' }));

    await waitFor(() => {
      expect(createAdminTenant).toHaveBeenCalledWith({ name: '租户 B', slug: 'tenant-b', is_internal: true });
    });
    expect(await screen.findByText('租户 B')).toBeTruthy();
    expect(promptSpy).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('keeps the create dialog open and shows backend error codes verbatim', async () => {
    vi.mocked(createAdminTenant).mockRejectedValue(new Error('admin_idempotency_conflict'));
    render(<TenantAdmin />);
    expect(await screen.findByText('租户 A')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '新建租户' }));
    await screen.findByRole('dialog');
    fireEvent.change(screen.getByLabelText('租户名称'), { target: { value: '租户 B' } });
    fireEvent.change(screen.getByLabelText('租户 slug'), { target: { value: 'tenant-b' } });
    fireEvent.click(screen.getByRole('button', { name: '创建' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('admin_idempotency_conflict');
  });

  it('shows loading and error states', async () => {
    vi.mocked(listAdminTenants).mockRejectedValue(new Error('boom'));
    render(<TenantAdmin />);
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
  });
});
