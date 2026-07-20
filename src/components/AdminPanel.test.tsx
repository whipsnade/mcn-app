import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiAdminUser } from '../api/contracts';
import {
  adjustAdminUserPoints,
  createAdminUser,
  getAdminUserPointsHistory,
  listAdminUsers,
  updateAdminUser,
} from '../api/admin';
import AdminPanel from './AdminPanel';

vi.mock('../api/admin', () => ({
  listAdminUsers: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
  deleteAdminUser: vi.fn(),
  adjustAdminUserPoints: vi.fn(),
  getAdminUserPointsHistory: vi.fn(),
}));

const mockListAdminUsers = vi.mocked(listAdminUsers);

const ADMIN_USER: ApiAdminUser = {
  id: 'u-admin',
  nickname: '系统管理员',
  role: 'admin',
  status: 'active',
  phone: '18888888888',
  points: 5000,
  reserved_points: 0,
  channels: ['xiaohongshu', 'douyin'],
  industries: ['美食'],
  created_at: '2026-01-01T00:00:00Z',
};

const NORMAL_USER: ApiAdminUser = {
  id: 'u-2',
  nickname: '运营小王',
  role: 'user',
  status: 'active',
  phone: '13812345678',
  points: 3450,
  reserved_points: 0,
  channels: ['xiaohongshu', 'bilibili'],
  industries: ['美食', '母婴'],
  created_at: '2026-06-15T00:00:00Z',
};

const renderPanel = () =>
  render(
    <AdminPanel
      isOpen
      onClose={vi.fn()}
      currentUserId={ADMIN_USER.id}
      currentUserNickname={ADMIN_USER.nickname}
    />,
  );

describe('AdminPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListAdminUsers.mockResolvedValue({ items: [ADMIN_USER, NORMAL_USER], total: 2 });
  });

  it('打开后展示 API 返回的用户列表', async () => {
    renderPanel();

    expect(await screen.findByText('运营小王')).toBeTruthy();
    expect(screen.getAllByText('系统管理员').length).toBeGreaterThan(0);
    expect(screen.getByText('18888888888')).toBeTruthy();
    expect(screen.getByText('13812345678')).toBeTruthy();
    expect(screen.getByText('积分: 5,000')).toBeTruthy();
    expect(screen.getByText('积分: 3,450')).toBeTruthy();
    // 渠道 slug 映射为中文标签
    expect(screen.getAllByText('小红书').length).toBeGreaterThan(0);
    expect(screen.getAllByText('抖音').length).toBeGreaterThan(0);
    expect(screen.getAllByText('B站').length).toBeGreaterThan(0);

    expect(mockListAdminUsers).toHaveBeenCalledWith({ keyword: undefined, channel: undefined, limit: 100 });
  });

  it('keyword 输入防抖后触发带 keyword 的重新请求', async () => {
    renderPanel();

    await screen.findByText('系统管理员');
    mockListAdminUsers.mockClear();

    fireEvent.change(screen.getByPlaceholderText('按用户名、手机号搜索...'), {
      target: { value: '小王' },
    });

    await waitFor(
      () => {
        expect(mockListAdminUsers).toHaveBeenCalledWith({ keyword: '小王', channel: undefined, limit: 100 });
      },
      { timeout: 2000 },
    );
  });

  it('当前登录账号的删除按钮保持禁用', async () => {
    renderPanel();

    await screen.findByText('运营小王');
    const deleteButtons = screen.getAllByTitle(/删除账号|无法删除/);
    const selfDelete = deleteButtons.find(btn => (btn as HTMLButtonElement).disabled);
    const otherDelete = deleteButtons.find(btn => !(btn as HTMLButtonElement).disabled);

    expect(selfDelete).toBeTruthy();
    expect(otherDelete).toBeTruthy();
  });

  it('列表加载失败时展示错误提示', async () => {
    mockListAdminUsers.mockRejectedValue(new Error('FORBIDDEN'));
    renderPanel();

    expect(await screen.findByText('FORBIDDEN')).toBeTruthy();
  });

  it('编辑自己时隐藏账号状态选项', async () => {
    renderPanel();

    await screen.findByText('运营小王');
    fireEvent.click(screen.getAllByTitle('管理/修改此账号信息')[0]); // 编辑自己

    expect(screen.queryByText('账号状态')).toBeNull();

    fireEvent.click(screen.getByText('关闭'));
    fireEvent.click(screen.getAllByTitle('管理/修改此账号信息')[1]); // 编辑他人

    expect(screen.getByText('账号状态')).toBeTruthy();
  });

  it('编辑保存时调用 updateAdminUser，积分变化时追加 adjustAdminUserPoints', async () => {
    vi.mocked(updateAdminUser).mockResolvedValue({ ...NORMAL_USER, points: 3950 });
    vi.mocked(adjustAdminUserPoints).mockResolvedValue({
      points: 3950,
      reserved_points: 0,
      transaction_id: 'txn-1',
    });
    renderPanel();

    await screen.findByText('运营小王');
    fireEvent.click(screen.getAllByTitle('管理/修改此账号信息')[1]);

    fireEvent.change(screen.getByPlaceholderText('设置可用积分上限'), {
      target: { value: '3950' },
    });
    fireEvent.click(screen.getByText('保存修改'));

    await waitFor(() => {
      expect(updateAdminUser).toHaveBeenCalledWith('u-2', expect.objectContaining({
        nickname: '运营小王',
        phone: '13812345678',
        role: 'user',
        status: 'active',
      }));
      expect(adjustAdminUserPoints).toHaveBeenCalledWith(
        'u-2',
        500,
        '管理后台积分调整',
        expect.any(String),
      );
    });
  });

  it('新增账号调用 createAdminUser 并展示后端错误', async () => {
    vi.mocked(createAdminUser).mockRejectedValue(new Error('PHONE_CONFLICT: 手机号已注册'));
    renderPanel();

    await screen.findByText('系统管理员');
    fireEvent.click(screen.getByText('新增账号'));

    fireEvent.change(screen.getByPlaceholderText('例如: 完美日记运营'), {
      target: { value: '新成员' },
    });
    fireEvent.change(screen.getByPlaceholderText('11位手机号码'), {
      target: { value: '13900001111' },
    });
    fireEvent.click(screen.getByText('立即添加'));

    expect(await screen.findByText('PHONE_CONFLICT: 手机号已注册')).toBeTruthy();
    expect(createAdminUser).toHaveBeenCalledWith(expect.objectContaining({
      nickname: '新成员',
      phone: '13900001111',
      channels: ['xiaohongshu', 'douyin'],
    }));
  });

  it('积分消耗面板映射 settle/赠送/调整条目', async () => {
    vi.mocked(getAdminUserPointsHistory).mockResolvedValue({
      total: 3,
      items: [
        { id: 'h1', kind: 'settle', points: 450, session_title: '完美日记推广', platform: 'xiaohongshu', created_at: '2026-07-12T08:00:00Z' },
        { id: 'h2', kind: 'welcome_grant', points: 1000, session_title: null, platform: null, created_at: '2026-06-15T08:00:00Z' },
        { id: 'h3', kind: 'admin_adjust', points: -200, session_title: null, platform: null, created_at: '2026-07-01T08:00:00Z' },
      ],
    });
    renderPanel();

    await screen.findByText('运营小王');
    fireEvent.click(screen.getAllByTitle('查看历史营销会话积分消耗状况')[1]);

    expect(await screen.findByText('完美日记推广')).toBeTruthy();
    expect(screen.getByText('新人积分赠送')).toBeTruthy();
    expect(screen.getByText('管理员积分调整')).toBeTruthy();
    expect(screen.getByText('总消耗: 450 积分')).toBeTruthy();
    expect(screen.getByText('-450')).toBeTruthy();
    expect(screen.getByText('+1000')).toBeTruthy();
    expect(screen.getByText('-200')).toBeTruthy();
    expect(getAdminUserPointsHistory).toHaveBeenCalledWith('u-2', { limit: 200 });
  });

  it('列表行展示行业属性 chips', async () => {
    renderPanel();

    await screen.findByText('运营小王');
    expect(screen.getAllByText('美食').length).toBeGreaterThan(0);
    expect(screen.getAllByText('母婴').length).toBeGreaterThan(0);
  });

  it('编辑表单回显行业，保存时随 PATCH 提交 industries', async () => {
    vi.mocked(updateAdminUser).mockResolvedValue({ ...NORMAL_USER, industries: ['美食', '汽车'] });
    renderPanel();

    await screen.findByText('运营小王');
    fireEvent.click(screen.getAllByTitle('管理/修改此账号信息')[1]);

    // 回显已有行业 chips（可移除）
    expect(screen.getByRole('button', { name: '移除行业 美食' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '移除行业 母婴' })).toBeTruthy();

    // 切换预设行业 + 自定义行业
    fireEvent.click(screen.getByRole('button', { name: /汽车/ }));
    fireEvent.change(screen.getByPlaceholderText('自定义行业（20 字内）'), {
      target: { value: '宠物' },
    });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByRole('button', { name: '移除行业 宠物' })).toBeTruthy();

    fireEvent.click(screen.getByText('保存修改'));

    await waitFor(() => {
      expect(updateAdminUser).toHaveBeenCalledWith('u-2', expect.objectContaining({
        industries: ['美食', '母婴', '汽车', '宠物'],
      }));
    });
  });

  it('自定义行业去重且最多 5 项', async () => {
    renderPanel();

    await screen.findByText('运营小王');
    fireEvent.click(screen.getAllByTitle('管理/修改此账号信息')[1]);

    // 重复添加已选行业不生效
    fireEvent.change(screen.getByPlaceholderText('自定义行业（20 字内）'), {
      target: { value: '美食' },
    });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getAllByRole('button', { name: '移除行业 美食' })).toHaveLength(1);

    // 已有 2 项，再选 3 项预设后达到上限，第 6 项被拒绝
    fireEvent.click(screen.getByRole('button', { name: /美妆/ }));
    fireEvent.click(screen.getByRole('button', { name: /汽车/ }));
    fireEvent.click(screen.getByRole('button', { name: /服饰/ }));
    fireEvent.click(screen.getByRole('button', { name: /家居/ }));

    expect(await screen.findByText('行业属性最多 5 项')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '移除行业 家居' })).toBeNull();
  });
});
