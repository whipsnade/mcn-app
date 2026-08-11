import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  adjustTenantWallet,
  getTenantWallet,
  listAdminTenantUsers,
  listTenantQuota,
  setTenantQuota,
  type AdminQuotaItem,
  type AdminTenantUser,
} from '../../api/adminGateway';
import TenantWalletQuota from './TenantWalletQuota';

vi.mock('../../api/adminGateway', () => ({
  listAdminTenantUsers: vi.fn(),
  listTenantQuota: vi.fn(),
  getTenantWallet: vi.fn(),
  adjustTenantWallet: vi.fn(),
  setTenantQuota: vi.fn(),
}));

const MEMBERS: AdminTenantUser[] = [
  { id: 'u-1', nickname: '成员甲', role: 'owner', status: 'active', created_at: '2026-08-01T00:00:00Z' },
  { id: 'u-2', nickname: '成员乙', role: 'member', status: 'active', created_at: '2026-08-02T00:00:00Z' },
];

const QUOTA: AdminQuotaItem[] = [
  { user_id: 'u-1', period: 'monthly', points_limit: 1000, status: 'active' },
];

function mockLoad(members: AdminTenantUser[] = MEMBERS, quota: AdminQuotaItem[] = QUOTA) {
  vi.mocked(listAdminTenantUsers).mockResolvedValue({ items: members, total: members.length, limit: 200, offset: 0 });
  vi.mocked(listTenantQuota).mockResolvedValue({ items: quota });
  vi.mocked(getTenantWallet).mockResolvedValue({ tenant_id: 'tenant-a', balance: 1500, reserved: 0 });
}

// 填写钱包调整表单并打开确认对话框。
async function openWalletDialog(delta: string, reason: string) {
  fireEvent.change(await screen.findByLabelText('调整成员'), { target: { value: 'u-1' } });
  fireEvent.change(screen.getByLabelText('调整积分'), { target: { value: delta } });
  fireEvent.change(screen.getByLabelText('调整原因'), { target: { value: reason } });
  fireEvent.click(screen.getByRole('button', { name: '调整钱包' }));
}

describe('TenantWalletQuota', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockLoad();
  });

  it('加载成员与周期额度并展示当前 points_limit', async () => {
    render(<TenantWalletQuota tenantId="tenant-a" />);

    expect(await screen.findByLabelText('成员甲 周期额度上限')).toHaveValue(1000);
    expect(screen.getByText('成员甲', { selector: 'td' })).toBeTruthy();
    // 未设置额度的成员显示占位输入
    expect(screen.getByLabelText('成员乙 周期额度上限')).toHaveValue(null);
    expect(listAdminTenantUsers).toHaveBeenCalledWith('tenant-a');
    expect(listTenantQuota).toHaveBeenCalledWith('tenant-a');
  });

  it('空成员列表展示 empty 状态', async () => {
    mockLoad([], []);
    render(<TenantWalletQuota tenantId="tenant-a" />);

    expect(await screen.findByText('暂无成员')).toBeTruthy();
  });

  it('加载失败展示错误', async () => {
    vi.mocked(listAdminTenantUsers).mockRejectedValue(new Error('tenant_not_found'));
    render(<TenantWalletQuota tenantId="tenant-a" />);

    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_not_found');
  });

  it('加载后首次调整的确认文案即显示真实余额', async () => {
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await openWalletDialog('500', '补偿');

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('当前余额 1500 → 调整后 2000 积分');
  });

  it('钱包只读 404（无钱包行）时确认文案退化为以服务端返回为准', async () => {
    vi.mocked(getTenantWallet).mockRejectedValue(new Error('tenant_wallet_not_found'));
    render(<TenantWalletQuota tenantId="tenant-a" />);
    // 成员与额度仍正常加载
    expect(await screen.findByLabelText('成员甲 周期额度上限')).toBeTruthy();

    await openWalletDialog('500', '补偿');
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('当前余额以服务端返回为准');
  });

  it('钱包只读的非 404 失败按错误模式展示', async () => {
    vi.mocked(getTenantWallet).mockRejectedValue(new Error('tenant_disabled'));
    render(<TenantWalletQuota tenantId="tenant-a" />);

    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_disabled');
  });

  it('钱包调整：取消确认不发请求', async () => {
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await openWalletDialog('500', '补偿');

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('补偿');
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));

    expect(adjustTenantWallet).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('钱包调整：确认后提交并展示交易 id，且刷新数据', async () => {
    vi.mocked(adjustTenantWallet).mockResolvedValue({
      tenant_id: 'tenant-a', balance: 1500, reserved: 0, transaction_id: 'txn-1',
    });
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await openWalletDialog('500', '补偿');

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '确认调整' }));

    await waitFor(() => {
      expect(adjustTenantWallet).toHaveBeenCalledWith('tenant-a', { user_id: 'u-1', delta: 500, reason: '补偿' });
    });
    expect(await screen.findByText(/txn-1/)).toBeTruthy();
    // 成功后刷新成员与额度
    await waitFor(() => expect(listTenantQuota).toHaveBeenCalledTimes(2));
  });

  it('钱包调整：已知余额时确认文案显示具体账务影响', async () => {
    vi.mocked(adjustTenantWallet).mockResolvedValue({
      tenant_id: 'tenant-a', balance: 1500, reserved: 0, transaction_id: 'txn-1',
    });
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await openWalletDialog('500', '补偿');
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '确认调整' }));
    await screen.findByText(/txn-1/);

    await openWalletDialog('-200', '扣回');
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('1500');
    expect(dialog).toHaveTextContent('1300');
  });

  it('钱包调整：失败展示错误且可重试', async () => {
    vi.mocked(adjustTenantWallet).mockRejectedValue(new Error('tenant_wallet_insufficient'));
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await openWalletDialog('-99999', '扣回');
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '确认调整' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_wallet_insufficient');
  });

  it('钱包调整：非法输入禁止提交', async () => {
    render(<TenantWalletQuota tenantId="tenant-a" />);
    await screen.findByLabelText('成员甲 周期额度上限');

    expect(screen.getByRole('button', { name: '调整钱包' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('调整成员'), { target: { value: 'u-1' } });
    fireEvent.change(screen.getByLabelText('调整积分'), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText('调整原因'), { target: { value: '补偿' } });
    // delta 为 0 不允许提交
    expect(screen.getByRole('button', { name: '调整钱包' })).toBeDisabled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('周期额度：编辑后经确认保存并刷新', async () => {
    vi.mocked(setTenantQuota).mockResolvedValue({ user_id: 'u-1', period: 'monthly', points_limit: 2000, status: 'active' });
    render(<TenantWalletQuota tenantId="tenant-a" />);

    fireEvent.change(await screen.findByLabelText('成员甲 周期额度上限'), { target: { value: '2000' } });
    fireEvent.click(screen.getByRole('button', { name: '保存成员甲 额度' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('只影响新周期/新 Run 的扣费上限');
    fireEvent.click(within(dialog).getByRole('button', { name: '确认保存' }));

    await waitFor(() => {
      expect(setTenantQuota).toHaveBeenCalledWith('tenant-a', 'u-1', { points_limit: 2000 });
    });
    await waitFor(() => expect(listTenantQuota).toHaveBeenCalledTimes(2));
  });

  it('周期额度：取消确认不发请求', async () => {
    render(<TenantWalletQuota tenantId="tenant-a" />);

    fireEvent.change(await screen.findByLabelText('成员甲 周期额度上限'), { target: { value: '2000' } });
    fireEvent.click(screen.getByRole('button', { name: '保存成员甲 额度' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));

    expect(setTenantQuota).not.toHaveBeenCalled();
  });

  it('周期额度：保存失败展示错误', async () => {
    vi.mocked(setTenantQuota).mockRejectedValue(new Error('tenant_membership_not_found'));
    render(<TenantWalletQuota tenantId="tenant-a" />);

    fireEvent.change(await screen.findByLabelText('成员乙 周期额度上限'), { target: { value: '300' } });
    fireEvent.click(screen.getByRole('button', { name: '保存成员乙 额度' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '确认保存' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('tenant_membership_not_found');
  });
});
