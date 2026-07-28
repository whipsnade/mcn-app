import { act, fireEvent, render, screen } from '@testing-library/react';
import { useEffect } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiQuickPlatform, ApiQuickTopPosts } from '../api/contracts';
import { getTopPosts } from '../api/quick';
import {
  QuickFeatureCacheProvider,
  useQuickFeatureCache,
  type QuickTopPostsCacheEntry,
} from '../state/QuickFeatureCache';
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

function renderPanel(platform: ApiQuickPlatform) {
  return render(
    <QuickFeatureCacheProvider userId="test-user">
      <TopPostsPanel platform={platform} />
    </QuickFeatureCacheProvider>,
  );
}

// 在挂载面板前向缓存写入指定平台的爆贴结果，模拟「上次查询过」的状态。
function SeedTopPosts({ platform, entry }: { platform: ApiQuickPlatform; entry: QuickTopPostsCacheEntry }) {
  const { setTopPosts } = useQuickFeatureCache();
  useEffect(() => {
    setTopPosts(platform, entry);
  }, [setTopPosts, platform, entry]);
  return null;
}

const DOUYIN_ENTRY: QuickTopPostsCacheEntry = {
  items: [
    {
      title: '抖音爆款第一条',
      nickname: '抖音达人',
      interact: 88_000,
      like: 60_000,
      comment: 5_000,
      collect: 2_000,
      publish_time: '2026-07-02T10:00:00Z',
      url: 'https://example.com/douyin-1',
      platform: 'douyin',
    },
  ],
  fallbackKols: [],
  degraded: false,
  pointsCost: 30,
  hasQueried: true,
};

describe('TopPostsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetTopPosts.mockResolvedValue(RESULT);
  });

  it('does not fetch on mount; fetches after clicking the query button', async () => {
    renderPanel('xiaohongshu');

    expect(screen.getByText(/点击右上角「查询\/刷新」/)).toBeTruthy();
    expect(mockGetTopPosts).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));

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
    renderPanel('douyin');

    expect(screen.getByText('抖音前十爆贴')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(await screen.findByText('年度必吃榜第一名')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledWith('douyin');
  });

  it('shows the insufficient-points hint on 409', async () => {
    mockGetTopPosts.mockRejectedValue(new Error('INSUFFICIENT_POINTS'));
    renderPanel('xiaohongshu');

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));

    expect(await screen.findByText('积分不足，请充值')).toBeTruthy();
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
    renderPanel('xiaohongshu');

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));

    expect(await screen.findByText(/爆贴数据服务暂不可用/)).toBeTruthy();
    expect(screen.getByText('美食小达人')).toBeTruthy();
    expect(screen.getByText('12.0万')).toBeTruthy();
    expect(screen.getByText('¥8,000')).toBeTruthy();
  });

  it('restores the cached douyin result on remount without fetching again', async () => {
    // 只卸载面板、保留 Provider，模拟切换 Tab 后再切回来。
    function Harness({ showPanel }: { showPanel: boolean }) {
      return (
        <QuickFeatureCacheProvider userId="test-user">
          {showPanel ? <TopPostsPanel platform="douyin" /> : null}
        </QuickFeatureCacheProvider>
      );
    }
    const view = render(<Harness showPanel />);

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(await screen.findByText('年度必吃榜第一名')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);

    view.rerender(<Harness showPanel={false} />);
    expect(screen.queryByText('年度必吃榜第一名')).toBeNull();
    view.rerender(<Harness showPanel />);

    // 缓存仍在：标题、积分与列表直接恢复，不再次请求
    expect(screen.getByText('抖音前十爆贴')).toBeTruthy();
    expect(screen.getByText(/消耗 10 积分/)).toBeTruthy();
    expect(screen.getByText('年度必吃榜第一名')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);
  });

  it('renders a cached result injected into the cache without calling the API', () => {
    render(
      <QuickFeatureCacheProvider userId="test-user">
        <SeedTopPosts platform="douyin" entry={DOUYIN_ENTRY} />
        <TopPostsPanel platform="douyin" />
      </QuickFeatureCacheProvider>,
    );

    expect(screen.getByText('抖音前十爆贴')).toBeTruthy();
    expect(screen.getByText(/消耗 30 积分/)).toBeTruthy();
    expect(screen.getByText('抖音爆款第一条')).toBeTruthy();
    expect(screen.getByText('抖音达人')).toBeTruthy();
    expect(mockGetTopPosts).not.toHaveBeenCalled();
  });

  it('keeps the loading state across unmount and remount while the query is in flight', async () => {
    let resolveQuery: (value: ApiQuickTopPosts) => void = () => undefined;
    mockGetTopPosts.mockImplementation(
      () => new Promise(resolve => {
        resolveQuery = resolve;
      }),
    );
    // 只卸载面板、保留 Provider，模拟切换 Tab 后再切回来。
    function Harness({ showPanel }: { showPanel: boolean }) {
      return (
        <QuickFeatureCacheProvider userId="test-user">
          {showPanel ? <TopPostsPanel platform="xiaohongshu" /> : null}
        </QuickFeatureCacheProvider>
      );
    }
    const view = render(<Harness showPanel />);

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(await screen.findByText(/正在加载爆贴榜单/)).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);

    view.rerender(<Harness showPanel={false} />);
    view.rerender(<Harness showPanel />);

    // 查询中的加载态从缓存恢复，而不是退回「未查询」空态
    expect(screen.getByText(/正在加载爆贴榜单/)).toBeTruthy();
    expect(screen.queryByText(/点击右上角「查询\/刷新」获取/)).toBeNull();

    // in-flight 期间再次点击不发起第二次请求
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveQuery(RESULT);
    });
    expect(await screen.findByText('年度必吃榜第一名')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);
  });

  it('keeps xiaohongshu and douyin cache entries isolated', () => {
    render(
      <QuickFeatureCacheProvider userId="test-user">
        <SeedTopPosts platform="douyin" entry={DOUYIN_ENTRY} />
        <TopPostsPanel platform="xiaohongshu" />
      </QuickFeatureCacheProvider>,
    );

    // 小红书面板读不到抖音缓存：仍处于未查询状态，也不展示抖音条目
    expect(screen.getByText(/点击右上角「查询\/刷新」/)).toBeTruthy();
    expect(screen.queryByText('抖音爆款第一条')).toBeNull();
    expect(mockGetTopPosts).not.toHaveBeenCalled();
  });
});
