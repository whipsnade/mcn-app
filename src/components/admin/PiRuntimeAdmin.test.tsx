import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { listAdminGateways, updateAdminGateway, type AdminGateway } from '../../api/adminGateway';
import PiRuntimeAdmin from './PiRuntimeAdmin';

vi.mock('../../api/adminGateway', () => ({
  listAdminGateways: vi.fn(),
  updateAdminGateway: vi.fn(),
}));

const GW_ACTIVE: AdminGateway = {
  id: 'row-1', gateway_id: 'gw-1', status: 'active', mode: 'active',
  desired_capacity: 4, last_seen_at: '2026-08-09T12:00:00Z', updated_at: '2026-08-09T12:00:00Z',
};

const GW_OFFLINE: AdminGateway = {
  id: 'row-2', gateway_id: 'gw-2', status: 'offline', mode: 'draining',
  desired_capacity: 2, last_seen_at: null, updated_at: '2026-08-09T11:00:00Z',
};

describe('PiRuntimeAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAdminGateways).mockResolvedValue({ items: [GW_ACTIVE, GW_OFFLINE], total: 2 });
  });

  it('lists gateways with textual status and mode badges', async () => {
    render(<PiRuntimeAdmin />);
    expect(await screen.findByText('gw-1')).toBeTruthy();
    expect(screen.getByText('gw-2')).toBeTruthy();
    // 状态与模式必须有文字徽标，不能只靠颜色
    expect(screen.getByText('在线')).toBeTruthy();
    expect(screen.getByText('离线')).toBeTruthy();
    expect(screen.getByText('接收新任务')).toBeTruthy();
    expect(screen.getByText('排空中')).toBeTruthy();
    expect(screen.getByText('从未上报')).toBeTruthy();
    const table = screen.getByRole('table', { name: 'Pi Gateway 实例列表' });
    expect(table.closest('div')?.className).toContain('overflow-x-auto');
  });

  it('shows an empty state', async () => {
    vi.mocked(listAdminGateways).mockResolvedValue({ items: [], total: 0 });
    render(<PiRuntimeAdmin />);
    expect(await screen.findByText('暂无 Gateway 实例')).toBeTruthy();
  });

  it('shows backend error codes verbatim', async () => {
    vi.mocked(listAdminGateways).mockRejectedValue(new Error('admin_idempotency_conflict'));
    render(<PiRuntimeAdmin />);
    expect(await screen.findByRole('alert')).toHaveTextContent('admin_idempotency_conflict');
  });

  it('switches mode to draining after a confirmation that explains claim semantics', async () => {
    vi.mocked(updateAdminGateway).mockResolvedValue({ ...GW_ACTIVE, mode: 'draining' });
    render(<PiRuntimeAdmin />);
    const row = (await screen.findByText('gw-1')).closest('tr') as HTMLElement;

    fireEvent.click(within(row).getByRole('button', { name: '切换为排空' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('只阻止新 claim');
    expect(dialog.textContent).toContain('在途 Worker 自然完成');
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(updateAdminGateway).not.toHaveBeenCalled();

    fireEvent.click(within(row).getByRole('button', { name: '切换为排空' }));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '确认切换' }));
    await waitFor(() => {
      expect(updateAdminGateway).toHaveBeenCalledWith('gw-1', { mode: 'draining' });
    });
  });

  it('resumes a draining gateway to active mode', async () => {
    vi.mocked(updateAdminGateway).mockResolvedValue({ ...GW_OFFLINE, mode: 'active' });
    render(<PiRuntimeAdmin />);
    const row = (await screen.findByText('gw-2')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: '切换为接收' }));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '确认切换' }));
    await waitFor(() => {
      expect(updateAdminGateway).toHaveBeenCalledWith('gw-2', { mode: 'active' });
    });
  });

  it('edits desired_capacity behind a confirmation dialog', async () => {
    vi.mocked(updateAdminGateway).mockResolvedValue({ ...GW_ACTIVE, desired_capacity: 8 });
    render(<PiRuntimeAdmin />);
    const row = (await screen.findByText('gw-1')).closest('tr') as HTMLElement;

    fireEvent.change(within(row).getByLabelText('gw-1 期望容量'), { target: { value: '8' } });
    fireEvent.click(within(row).getByRole('button', { name: '应用容量' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('期望容量');
    fireEvent.click(screen.getByRole('button', { name: '确认调整' }));
    await waitFor(() => {
      expect(updateAdminGateway).toHaveBeenCalledWith('gw-1', { desired_capacity: 8 });
    });
  });
});
