import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiFavorite } from '../api/contracts';
import { deleteFavorite, deleteFavoriteByKey } from '../api/favorites';
import FavoritesPanel from './FavoritesPanel';

vi.mock('../api/favorites', () => ({
  deleteFavorite: vi.fn(),
  deleteFavoriteByKey: vi.fn(),
}));

function favoriteFixture(overrides: Partial<ApiFavorite> = {}): ApiFavorite {
  return {
    id: 'fav-1',
    kol_id: null,
    platform: 'xiaohongshu',
    platform_account_id: null,
    kol_uid: 'uid-1',
    nickname: '达人小A',
    profile_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
    snapshot: { followers: 120000, quoted_price_cny: 12000, city: '上海' },
    note: null,
    source_task_id: null,
    created_at: '2026-07-20T10:00:00Z',
    ...overrides,
  };
}

const LEGACY_ROW = favoriteFixture({
  id: 'fav-2',
  kol_id: 'kol-a',
  platform: 'douyin',
  platform_account_id: 'dy-a',
  kol_uid: null,
  nickname: '探店达人乙',
  profile_url: null,
  snapshot: null,
  source_task_id: 'task-1',
});

const PRICE_ROW = favoriteFixture({
  id: 'fav-3',
  platform: 'douyin',
  kol_uid: 'uid-3',
  nickname: '推荐达人丙',
  snapshot: { followers: 8000, price: 3000, engagement_rate: 5.2 },
});

describe('FavoritesPanel', () => {
  beforeEach(() => {
    vi.mocked(deleteFavorite).mockReset().mockResolvedValue();
    vi.mocked(deleteFavoriteByKey).mockReset().mockResolvedValue();
  });

  it('renders controlled favorites with platform name and snapshot details', () => {
    render(<FavoritesPanel favorites={[favoriteFixture(), LEGACY_ROW, PRICE_ROW]} loading={false} />);

    expect(screen.getByText('达人小A')).toBeVisible();
    expect(screen.getAllByText(/小红书/).length).toBeGreaterThan(0);
    expect(screen.getByText(/粉丝 12万/)).toBeVisible();
    expect(screen.getByText(/¥12,000/)).toBeVisible();
    // 快捷推荐路径的快照使用 price 键
    expect(screen.getByText(/¥3,000/)).toBeVisible();
    // 无快照的旧路径行正常降级
    expect(screen.getByText('探店达人乙')).toBeVisible();
  });

  it('reports the favorite count through onCountChange', () => {
    const onCountChange = vi.fn();
    render(
      <FavoritesPanel favorites={[favoriteFixture(), LEGACY_ROW]} loading={false} onCountChange={onCountChange} />,
    );

    expect(onCountChange).toHaveBeenCalledWith(2);
  });

  it('removes a platform+kol_uid row through deleteFavoriteByKey and refreshes', async () => {
    const onRefresh = vi.fn();
    render(<FavoritesPanel favorites={[favoriteFixture()]} loading={false} onRefresh={onRefresh} />);

    fireEvent.click(screen.getByRole('button', { name: '取消收藏 达人小A' }));

    await waitFor(() => expect(deleteFavoriteByKey).toHaveBeenCalledWith('xiaohongshu', 'uid-1'));
    expect(deleteFavorite).not.toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('removes a legacy kol_id row through deleteFavorite and refreshes', async () => {
    const onRefresh = vi.fn();
    render(<FavoritesPanel favorites={[LEGACY_ROW]} loading={false} onRefresh={onRefresh} />);

    fireEvent.click(screen.getByRole('button', { name: '取消收藏 探店达人乙' }));

    await waitFor(() => expect(deleteFavorite).toHaveBeenCalledWith('kol-a'));
    expect(deleteFavoriteByKey).not.toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('shows an inline error when removal fails and keeps the row', async () => {
    vi.mocked(deleteFavoriteByKey).mockRejectedValueOnce(new Error('network'));
    render(<FavoritesPanel favorites={[favoriteFixture()]} loading={false} />);

    fireEvent.click(screen.getByRole('button', { name: '取消收藏 达人小A' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('取消收藏失败');
    expect(screen.getByText('达人小A')).toBeVisible();
  });

  it('shows the loading hint while favorites are being fetched', () => {
    render(<FavoritesPanel favorites={[]} loading />);

    expect(screen.getByText('正在加载收藏…')).toBeVisible();
  });

  it('keeps the empty state copy when there are no favorites', () => {
    render(<FavoritesPanel favorites={[]} loading={false} />);

    expect(screen.getByText('还没有收藏的达人')).toBeVisible();
  });
});
