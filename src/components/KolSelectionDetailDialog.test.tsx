import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getKolSelectionDetail, queryKolSelectionDetail } from '../api/kolSelection';
import type { KolSelectionItem } from '../api/kolSelection';
import KolSelectionDetailDialog from './KolSelectionDetailDialog';

vi.mock('../api/kolSelection', () => ({
  getKolSelectionDetail: vi.fn(),
  queryKolSelectionDetail: vi.fn(),
}));

const item: KolSelectionItem = {
  platform: 'xiaohongshu',
  kol_uid: 'xhs-1',
  nickname: '美食小达人',
  followers: 120000,
  city: '上海',
  profile_url: 'https://example.com/profile/xhs-1',
  fields: { engagement_rate: 4.2, quoted_price_cny: 12000 },
  score: { total: 85, rating: '重点推荐' },
};

const queried = {
  set_id: 'set-1',
  platform: 'xiaohongshu',
  kol_uid: 'xhs-1',
  source: 'query' as const,
  points_cost: 20,
  posts_degraded: false,
  fetched_at: '2026-07-29T10:00:00',
  detail: {
    nickname: '美食小达人',
    followers: 130000,
    profile_url: 'https://example.com/profile/xhs-1',
    engagement_rate: 4.8,
    effective_follower_rate: 63,
    audience_age: { '18-24': 42, '25-34': 37 },
    audience_regions: { 上海: 31, 浙江: 18 },
    audience_interests: { 美食: 62, 生活: 24 },
    trend_points: [
      { week_start: '2026-07-01', average_interactions: 4200, post_count: 3 },
      { week_start: '2026-07-08', average_interactions: 5600, post_count: 4 },
    ],
    score: { total: 85, rating: '重点推荐' },
  },
  posts: [{ title: '夏日探店', url: 'https://example.com/post-1', interact: 68000, like: 52000, comment: 3200, publish_time: '2026-07-15', platform: 'xiaohongshu' }],
};

describe('KolSelectionDetailDialog', () => {
  beforeEach(() => {
    vi.mocked(getKolSelectionDetail).mockReset();
    vi.mocked(queryKolSelectionDetail).mockReset();
  });

  it('缓存缺失时自动查询并展示 BI 图表、主页和五条以内热帖', async () => {
    vi.mocked(getKolSelectionDetail).mockResolvedValue({
      ...queried,
      source: 'missing',
      points_cost: 0,
      fetched_at: null,
      posts: [],
      detail: { nickname: '美食小达人', followers: 120000 },
    });
    vi.mocked(queryKolSelectionDetail).mockResolvedValue(queried);

    render(<KolSelectionDetailDialog sessionId="session-1" setId="set-1" item={item} onClose={vi.fn()} />);

    expect(await screen.findByRole('dialog', { name: '美食小达人达人详情' })).toBeVisible();
    await waitFor(() => expect(queryKolSelectionDetail).toHaveBeenCalledWith('session-1', {
      set_id: 'set-1', platform: 'xiaohongshu', kol_uid: 'xhs-1', refresh: false,
    }));
    expect(screen.getByText('互动趋势')).toBeVisible();
    expect(screen.getByText('受众地区')).toBeVisible();
    expect(screen.getByRole('link', { name: '打开主页' })).toHaveAttribute('href', 'https://example.com/profile/xhs-1');
    expect(screen.getByRole('link', { name: '查看热帖：夏日探店' })).toHaveAttribute('href', 'https://example.com/post-1');
    expect(screen.getByText('本次查询消耗 20 积分')).toBeVisible();
  });

  it('缓存命中不再查询，点击刷新才重新查询并显示实际积分', async () => {
    vi.mocked(getKolSelectionDetail).mockResolvedValue({ ...queried, source: 'cache', points_cost: 0 });
    vi.mocked(queryKolSelectionDetail).mockResolvedValue({ ...queried, source: 'refresh', points_cost: 20 });

    render(<KolSelectionDetailDialog sessionId="session-1" setId="set-1" item={item} onClose={vi.fn()} />);

    await screen.findByText('来自缓存，不消耗积分');
    expect(queryKolSelectionDetail).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '刷新达人详情' }));
    await waitFor(() => expect(queryKolSelectionDetail).toHaveBeenCalledWith('session-1', {
      set_id: 'set-1', platform: 'xiaohongshu', kol_uid: 'xhs-1', refresh: true,
    }));
    expect(await screen.findByText(/本次查询消耗 20 积分/)).toBeVisible();
  });

  it('刷新失败且已有缓存时保留旧数据并展示错误横幅', async () => {
    vi.mocked(getKolSelectionDetail).mockResolvedValue({ ...queried, source: 'cache', points_cost: 0 });
    vi.mocked(queryKolSelectionDetail).mockRejectedValue(new Error('上游服务暂时不可用'));

    render(<KolSelectionDetailDialog sessionId="session-1" setId="set-1" item={item} onClose={vi.fn()} />);

    await screen.findByText('来自缓存，不消耗积分');
    fireEvent.click(screen.getByRole('button', { name: '刷新达人详情' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('达人详情加载失败，请稍后重试');
    // 旧缓存内容仍展示（不全屏错误）
    expect(screen.getByText('互动趋势')).toBeVisible();
    expect(screen.getByText(/以下为已缓存的数据/)).toBeVisible();
  });
});
