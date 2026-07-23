import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiFavorite, ApiQuickKolRecommendations } from '../api/contracts';
import { createFavoriteByKey, deleteFavoriteByKey } from '../api/favorites';
import { getKolRecommendations } from '../api/quick';
import KolRecommendPanel from './KolRecommendPanel';

vi.mock('../api/quick', () => ({
  getKolRecommendations: vi.fn(),
  quickErrorMessage: (error: unknown) =>
    error instanceof Error && error.message === 'INSUFFICIENT_POINTS' ? '积分不足，请充值' : '查询失败，请稍后重试',
}));

vi.mock('../api/favorites', () => ({
  createFavoriteByKey: vi.fn(),
  deleteFavoriteByKey: vi.fn(),
}));

const mockGetKolRecommendations = vi.mocked(getKolRecommendations);
const mockCreateFavoriteByKey = vi.mocked(createFavoriteByKey);
const mockDeleteFavoriteByKey = vi.mocked(deleteFavoriteByKey);

function favoriteFixture(overrides: Partial<ApiFavorite> = {}): ApiFavorite {
  return {
    id: 'fav-1',
    kol_id: null,
    platform: 'xiaohongshu',
    platform_account_id: null,
    kol_uid: 'uid-1',
    nickname: '美食达人甲',
    profile_url: null,
    snapshot: null,
    note: null,
    source_task_id: null,
    created_at: '2026-07-20T10:00:00Z',
    ...overrides,
  };
}

const RESULT: ApiQuickKolRecommendations = {
  items: [
    {
      platform: 'xiaohongshu',
      kw_uid: 'uid-1',
      nickname: '美食达人甲',
      fans: 125_000,
      price: 30_000,
      engagement_rate: 5.2,
      score: 88,
      city: '上海',
      tags: ['美食'],
    },
    {
      platform: 'douyin',
      kw_uid: 'uid-2',
      nickname: '探店达人乙',
      fans: 8_000,
      price: null,
      engagement_rate: null,
      score: 70,
      city: null,
      tags: [],
    },
  ],
  points_cost: 20,
};

async function advanceDebounce(ms = 800) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('KolRecommendPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mockGetKolRecommendations.mockResolvedValue(RESULT);
    mockCreateFavoriteByKey.mockReset();
    mockDeleteFavoriteByKey.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not fetch on mount; fetches after clicking the query button', async () => {
    render(<KolRecommendPanel onSelectKol={vi.fn()} />);

    expect(screen.getByText(/点击右上角「查询\/刷新」/)).toBeTruthy();
    await advanceDebounce();
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 50_000 });
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(screen.getByText('探店达人乙')).toBeTruthy();
    expect(screen.getByText('粉丝 12.5万')).toBeTruthy();
    expect(screen.getByText('¥30,000')).toBeTruthy();
    expect(screen.getByText('无报价')).toBeTruthy();
    expect(screen.getByText('互动率 5.2%')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();
  });

  it('debounces slider changes only after the first manual query', async () => {
    render(<KolRecommendPanel onSelectKol={vi.fn()} />);

    // 未手动查询前拖动滑动条不触发请求
    fireEvent.change(screen.getByLabelText('单达人报价预算'), { target: { value: '200000' } });
    await advanceDebounce();
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    mockGetKolRecommendations.mockClear();

    const slider = screen.getByLabelText('单达人报价预算');
    fireEvent.change(slider, { target: { value: '200000' } });
    fireEvent.change(slider, { target: { value: '300000' } });

    // 防抖窗口内连续变更只触发一次请求
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    await advanceDebounce(400);
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 300_000 });
    expect(screen.getByText('¥30.0万')).toBeTruthy();
  });

  it('reports the selected kol when a row is clicked', async () => {
    const onSelectKol = vi.fn();
    render(<KolRecommendPanel onSelectKol={onSelectKol} />);
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    fireEvent.click(screen.getByText('美食达人甲'));

    expect(onSelectKol).toHaveBeenCalledWith({
      platform: 'xiaohongshu',
      kw_uid: 'uid-1',
      nickname: '美食达人甲',
    });
  });

  it('shows a recharge hint when points are insufficient', async () => {
    mockGetKolRecommendations.mockRejectedValue(new Error('INSUFFICIENT_POINTS'));
    render(<KolRecommendPanel onSelectKol={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    expect(screen.getByText('积分不足，请充值')).toBeTruthy();
  });

  it('creates a favorite from a recommendation card with a defensive snapshot', async () => {
    mockCreateFavoriteByKey.mockResolvedValue(favoriteFixture());
    const onFavoriteToggled = vi.fn();
    render(<KolRecommendPanel onSelectKol={vi.fn()} favorites={[]} onFavoriteToggled={onFavoriteToggled} />);
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    const stars = screen.getAllByRole('button', { name: '收藏' });
    expect(stars).toHaveLength(2);
    await act(async () => {
      fireEvent.click(stars[0]);
    });

    expect(mockCreateFavoriteByKey).toHaveBeenCalledWith({
      platform: 'xiaohongshu',
      kolUid: 'uid-1',
      nickname: '美食达人甲',
      snapshot: { followers: 125_000, price: 30_000, engagement_rate: 5.2, city: '上海' },
    });
    expect(onFavoriteToggled).toHaveBeenCalledTimes(1);
  });

  it('omits null fields from the snapshot', async () => {
    mockCreateFavoriteByKey.mockResolvedValue(favoriteFixture());
    render(<KolRecommendPanel onSelectKol={vi.fn()} favorites={[]} onFavoriteToggled={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: '收藏' })[1]);
    });

    expect(mockCreateFavoriteByKey).toHaveBeenCalledWith({
      platform: 'douyin',
      kolUid: 'uid-2',
      nickname: '探店达人乙',
      snapshot: { followers: 8_000 },
    });
  });

  it('marks favorited items as active and removes them through deleteFavoriteByKey', async () => {
    mockDeleteFavoriteByKey.mockResolvedValue();
    const onFavoriteToggled = vi.fn();
    render(
      <KolRecommendPanel
        onSelectKol={vi.fn()}
        favorites={[favoriteFixture({ platform: 'douyin', kol_uid: 'uid-2', nickname: '探店达人乙' })]}
        onFavoriteToggled={onFavoriteToggled}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    expect(screen.getAllByRole('button', { name: '收藏' })).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '取消收藏' }));
    });

    expect(mockDeleteFavoriteByKey).toHaveBeenCalledWith('douyin', 'uid-2');
    expect(mockCreateFavoriteByKey).not.toHaveBeenCalled();
    expect(onFavoriteToggled).toHaveBeenCalledTimes(1);
  });
});
