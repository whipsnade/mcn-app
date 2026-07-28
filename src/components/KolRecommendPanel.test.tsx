import { act, fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiFavorite, ApiQuickKolRecommendations } from '../api/contracts';
import { createFavoriteByKey, deleteFavoriteByKey } from '../api/favorites';
import { getKolRecommendations } from '../api/quick';
import { QuickFeatureCacheProvider } from '../state/QuickFeatureCache';
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

// 默认全选的五平台（顺序与 QUICK_PLATFORMS 一致）
const ALL_PLATFORMS = ['xiaohongshu', 'douyin', 'bilibili', 'weibo', 'wechat'];

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

function renderPanel(ui: ReactElement) {
  return render(<QuickFeatureCacheProvider userId="test-user">{ui}</QuickFeatureCacheProvider>);
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
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

    expect(screen.getByText(/点击右上角「查询\/刷新」/)).toBeTruthy();
    await advanceDebounce();
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 50_000, platforms: ALL_PLATFORMS });
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(screen.getByText('探店达人乙')).toBeTruthy();
    expect(screen.getByText('粉丝 12.5万')).toBeTruthy();
    expect(screen.getByText('¥30,000')).toBeTruthy();
    expect(screen.getByText('无报价')).toBeTruthy();
    expect(screen.getByText('互动率 5.2%')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();
  });

  it('debounces slider changes only after the first manual query', async () => {
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

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
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 300_000, platforms: ALL_PLATFORMS });
    expect(screen.getByText('¥30.0万')).toBeTruthy();
  });

  it('restores budget and results from cache after remount without fetching again', async () => {
    // 只卸载面板、保留 Provider，模拟切换快捷 Tab 后再切回来。
    function Harness({ showPanel }: { showPanel: boolean }) {
      return (
        <QuickFeatureCacheProvider userId="test-user">
          {showPanel ? <KolRecommendPanel onSelectKol={vi.fn()} /> : null}
        </QuickFeatureCacheProvider>
      );
    }
    const view = render(<Harness showPanel />);

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(screen.getByText('美食达人甲')).toBeTruthy();

    // 调整预算并让防抖查询完成，随后卸载面板
    fireEvent.change(screen.getByLabelText('单达人报价预算'), { target: { value: '200000' } });
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 200_000, platforms: ALL_PLATFORMS });
    view.rerender(<Harness showPanel={false} />);
    mockGetKolRecommendations.mockClear();

    view.rerender(<Harness showPanel />);

    // 预算、名单与积分消耗都从缓存恢复，且不再发起请求
    expect(screen.getByText('¥20.0万')).toBeTruthy();
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(screen.getByText('探店达人乙')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();
    await advanceDebounce();
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    // 重挂载后再修改预算：防抖间隔后仅发起一次新请求
    fireEvent.change(screen.getByLabelText('单达人报价预算'), { target: { value: '300000' } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();
    await advanceDebounce(400);
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 300_000, platforms: ALL_PLATFORMS });
  });

  it('拖拽预算后防抖窗口内切 Tab（卸载），切回后按预算差异补查恰好一次', async () => {
    // 只卸载面板、保留 Provider，模拟切换快捷 Tab。
    function Harness({ showPanel }: { showPanel: boolean }) {
      return (
        <QuickFeatureCacheProvider userId="test-user">
          {showPanel ? <KolRecommendPanel onSelectKol={vi.fn()} /> : null}
        </QuickFeatureCacheProvider>
      );
    }
    const view = render(<Harness showPanel />);

    // 首次手动查询（预算 ¥5.0万）
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenLastCalledWith({ budget: 50_000, platforms: ALL_PLATFORMS });
    expect(screen.getByText('美食达人甲')).toBeTruthy();

    // 拖到 ¥20.0万，防抖窗口内卸载面板：防抖 timer 被 cleanup 清掉，查询从未发出
    fireEvent.change(screen.getByLabelText('单达人报价预算'), { target: { value: '200000' } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    view.rerender(<Harness showPanel={false} />);
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    mockGetKolRecommendations.mockClear();

    // 切回：滑动条显示新预算（缓存），经过防抖后以新预算补查恰好一次
    view.rerender(<Harness showPanel />);
    expect(screen.getByText('¥20.0万')).toBeTruthy();
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 200_000, platforms: ALL_PLATFORMS });

    // 补查完成后预算与已查询预算一致，不再重复请求
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
  });

  it('in-flight 查询中卸载/重挂载 loading 保留，且不重复请求', async () => {
    let resolveQuery: (value: ApiQuickKolRecommendations) => void = () => undefined;
    mockGetKolRecommendations.mockImplementation(
      () => new Promise(resolve => {
        resolveQuery = resolve;
      }),
    );
    // 只卸载面板、保留 Provider，模拟切换快捷 Tab 后再切回来。
    function Harness({ showPanel }: { showPanel: boolean }) {
      return (
        <QuickFeatureCacheProvider userId="test-user">
          {showPanel ? <KolRecommendPanel onSelectKol={vi.fn()} /> : null}
        </QuickFeatureCacheProvider>
      );
    }
    const view = render(<Harness showPanel />);

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/正在加载达人推荐/)).toBeTruthy();

    view.rerender(<Harness showPanel={false} />);
    view.rerender(<Harness showPanel />);

    // loading 态从缓存恢复；in-flight 期间再次点击与防抖窗口都不重复请求
    expect(screen.getByText(/正在加载达人推荐/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveQuery(RESULT);
    });
    // fake timers 下不用 findByText（waitFor 轮询依赖计时器），act 刷新后同步断言
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
  });

  it('in-flight 期间拖动预算，settle 后按新预算恰好补查一次', async () => {
    let resolveQuery: (value: ApiQuickKolRecommendations) => void = () => undefined;
    mockGetKolRecommendations.mockImplementationOnce(
      () => new Promise(resolve => {
        resolveQuery = resolve;
      }),
    );
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

    // 首次手动查询（预算 ¥5.0万），请求保持 in-flight
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenLastCalledWith({ budget: 50_000, platforms: ALL_PLATFORMS });
    expect(screen.getByText(/正在加载达人推荐/)).toBeTruthy();

    // in-flight 期间把预算拖到 ¥20.0万：loading 守卫生效，防抖窗口内不发新请求
    fireEvent.change(screen.getByLabelText('单达人报价预算'), { target: { value: '200000' } });
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);

    // 第一次查询 settle：loading 复位后 effect 重跑，按预算差异经防抖补查恰好一次
    await act(async () => {
      resolveQuery(RESULT);
    });
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);

    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(2);
    expect(mockGetKolRecommendations).toHaveBeenLastCalledWith({ budget: 200_000, platforms: ALL_PLATFORMS });

    // 补查完成后预算与已查询预算一致，不再出现第三次请求
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(2);
  });

  it('reports the selected kol when a row is clicked', async () => {
    const onSelectKol = vi.fn();
    renderPanel(<KolRecommendPanel onSelectKol={onSelectKol} />);
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
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();

    expect(screen.getByText('积分不足，请充值')).toBeTruthy();
  });

  it('creates a favorite from a recommendation card with a defensive snapshot', async () => {
    mockCreateFavoriteByKey.mockResolvedValue(favoriteFixture());
    const onFavoriteToggled = vi.fn();
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} favorites={[]} onFavoriteToggled={onFavoriteToggled} />);
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
    renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} favorites={[]} onFavoriteToggled={vi.fn()} />);
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
    renderPanel(
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

  describe('平台多选', () => {
    it('平台 chips 默认全选，点击可切换选中态（aria-pressed）', () => {
      renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

      for (const label of ['小红书', '抖音', 'B站', '微博', '微信']) {
        expect(screen.getByRole('button', { name: label })).toHaveAttribute('aria-pressed', 'true');
      }

      fireEvent.click(screen.getByRole('button', { name: 'B站' }));
      expect(screen.getByRole('button', { name: 'B站' })).toHaveAttribute('aria-pressed', 'false');

      fireEvent.click(screen.getByRole('button', { name: 'B站' }));
      expect(screen.getByRole('button', { name: 'B站' })).toHaveAttribute('aria-pressed', 'true');
    });

    it('全不选时「查询/刷新」禁用', () => {
      renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

      for (const label of ['小红书', '抖音', 'B站', '微博', '微信']) {
        fireEvent.click(screen.getByRole('button', { name: label }));
      }

      expect(screen.getByRole('button', { name: /查询\/刷新/ })).toBeDisabled();

      // 重新勾选一个后恢复可用
      fireEvent.click(screen.getByRole('button', { name: '小红书' }));
      expect(screen.getByRole('button', { name: /查询\/刷新/ })).toBeEnabled();
    });

    it('查询时将选中的 platforms 传给后端', async () => {
      renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

      fireEvent.click(screen.getByRole('button', { name: '微博' }));
      fireEvent.click(screen.getByRole('button', { name: '微信' }));
      fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
      await advanceDebounce();

      expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
      expect(mockGetKolRecommendations).toHaveBeenCalledWith({
        budget: 50_000,
        platforms: ['xiaohongshu', 'douyin', 'bilibili'],
      });
    });

    it('平台选择存入缓存，切 Tab（卸载/重挂载）后保留', async () => {
      function Harness({ showPanel }: { showPanel: boolean }) {
        return (
          <QuickFeatureCacheProvider userId="test-user">
            {showPanel ? <KolRecommendPanel onSelectKol={vi.fn()} /> : null}
          </QuickFeatureCacheProvider>
        );
      }
      const view = render(<Harness showPanel />);

      fireEvent.click(screen.getByRole('button', { name: '微博' }));
      fireEvent.click(screen.getByRole('button', { name: '微信' }));
      view.rerender(<Harness showPanel={false} />);
      view.rerender(<Harness showPanel />);

      expect(screen.getByRole('button', { name: '微博' })).toHaveAttribute('aria-pressed', 'false');
      expect(screen.getByRole('button', { name: '微信' })).toHaveAttribute('aria-pressed', 'false');
      expect(screen.getByRole('button', { name: '小红书' })).toHaveAttribute('aria-pressed', 'true');
      // 全不选禁用状态也应从缓存恢复
      for (const label of ['小红书', '抖音', 'B站']) {
        fireEvent.click(screen.getByRole('button', { name: label }));
      }
      expect(screen.getByRole('button', { name: /查询\/刷新/ })).toBeDisabled();
    });

    it('改变平台选择后触发重新查询（排序后比较）', async () => {
      renderPanel(<KolRecommendPanel onSelectKol={vi.fn()} />);

      fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
      await advanceDebounce();
      expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
      mockGetKolRecommendations.mockClear();

      // 取消一个平台：防抖后按新平台组合重新查询
      fireEvent.click(screen.getByRole('button', { name: '微博' }));
      await advanceDebounce();
      expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
      expect(mockGetKolRecommendations).toHaveBeenLastCalledWith({
        budget: 50_000,
        platforms: ['xiaohongshu', 'douyin', 'bilibili', 'wechat'],
      });

      // 选回微博：平台组合与最近一次 queriedPlatforms（4 平台）不同，再次触发查询
      fireEvent.click(screen.getByRole('button', { name: '微博' }));
      await advanceDebounce();
      expect(mockGetKolRecommendations).toHaveBeenCalledTimes(2);
      expect(mockGetKolRecommendations).toHaveBeenLastCalledWith({
        budget: 50_000,
        platforms: ALL_PLATFORMS,
      });

      // 组合稳定后不再重复请求
      await advanceDebounce();
      expect(mockGetKolRecommendations).toHaveBeenCalledTimes(2);
    });
  });
});
