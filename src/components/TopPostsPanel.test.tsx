import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiQuickTopPosts } from '../api/contracts';
import { getTopPosts } from '../api/quick';
import TopPostsPanel from './TopPostsPanel';

vi.mock('../api/quick', () => ({
  getTopPosts: vi.fn(),
  quickErrorMessage: (error: unknown) =>
    error instanceof Error && error.message === 'INSUFFICIENT_POINTS' ? '积分不足，请充值' : '查询失败，请稍后重试',
}));

const mockGetTopPosts = vi.mocked(getTopPosts);

const RESULT: ApiQuickTopPosts = {
  items: [
    {
      title: '年度必吃榜第一名',
      nickname: '吃货小分队',
      interact: 152_000,
      like: 98_000,
      comment: 12_000,
      collect: 8_000,
      publish_time: '2026-07-01T10:00:00Z',
      url: 'https://example.com/post-1',
      platform: 'xiaohongshu',
    },
    {
      title: '隐藏菜单大公开',
      nickname: '探店老王',
      interact: 5_000,
      like: 3_000,
      comment: 200,
      collect: 100,
      publish_time: null,
      url: null,
      platform: 'xiaohongshu',
    },
  ],
  points_cost: 10,
};

describe('TopPostsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetTopPosts.mockResolvedValue(RESULT);
  });

  it('requests the platform leaderboard and renders the table rows', async () => {
    render(<TopPostsPanel platform="xiaohongshu" onBack={vi.fn()} />);

    expect(await screen.findByText('年度必吃榜第一名')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledWith('xiaohongshu');
    expect(screen.getByText('小红书前十爆贴')).toBeTruthy();
    expect(screen.getByText('吃货小分队')).toBeTruthy();
    expect(screen.getByText('15.2万')).toBeTruthy();
    expect(screen.getByText('9.8万')).toBeTruthy();
    const expectedDate = new Date('2026-07-01T10:00:00Z')
      .toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
    expect(screen.getByText(expectedDate)).toBeTruthy();
    const link = screen.getByRole('link', { name: '查看' });
    expect(link).toHaveAttribute('href', 'https://example.com/post-1');
    expect(link).toHaveAttribute('target', '_blank');
    // 无链接的帖子回退为占位符
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('shows the douyin title for the douyin platform', async () => {
    render(<TopPostsPanel platform="douyin" onBack={vi.fn()} />);

    expect(await screen.findByText('抖音前十爆贴')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledWith('douyin');
  });

  it('shows the insufficient-points hint on 409', async () => {
    mockGetTopPosts.mockRejectedValue(new Error('INSUFFICIENT_POINTS'));
    render(<TopPostsPanel platform="xiaohongshu" onBack={vi.fn()} />);

    expect(await screen.findByText('积分不足，请充值')).toBeTruthy();
  });

  it('goes back to the session view', async () => {
    const onBack = vi.fn();
    render(<TopPostsPanel platform="xiaohongshu" onBack={onBack} />);

    fireEvent.click(screen.getByRole('button', { name: /返回会话/ }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it('renders the degraded hot-kol fallback when posts are unavailable', async () => {
    mockGetTopPosts.mockResolvedValue({
      items: [],
      points_cost: 20,
      degraded: true,
      fallback_kols: [
        {
          platform: 'xiaohongshu',
          kw_uid: 'xhs-1',
          nickname: '美食小达人',
          fans: 120000,
          price: 8000,
          engagement_rate: 0.05,
          score: 88.5,
          city: '上海市',
          tags: [],
        },
      ],
    });
    render(<TopPostsPanel platform="xiaohongshu" onBack={vi.fn()} />);

    expect(await screen.findByText(/爆贴数据服务暂不可用/)).toBeTruthy();
    expect(screen.getByText('美食小达人')).toBeTruthy();
    expect(screen.getByText('12.0万')).toBeTruthy();
    expect(screen.getByText('¥8,000')).toBeTruthy();
  });
});
