import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiQuickTopPost, ApiQuickTopPosts } from '../api/contracts';
import { getKolRecommendations, getTopPosts } from '../api/quick';
import {
  QuickFeatureCacheProvider,
  useQuickFeatureCache,
  type QuickTopPostsCacheEntry,
} from './QuickFeatureCache';

vi.mock('../api/quick', () => ({
  getTopPosts: vi.fn(),
  getKolRecommendations: vi.fn(),
  quickErrorMessage: () => '查询失败，请稍后重试',
}));

const mockGetTopPosts = vi.mocked(getTopPosts);
const mockGetKolRecommendations = vi.mocked(getKolRecommendations);

const douyinEntry: QuickTopPostsCacheEntry = {
  items: [
    {
      title: '示例爆贴',
      nickname: '达人A',
      interact: 1234,
      like: 100,
      comment: 20,
      collect: 5,
      publish_time: '2026-07-01',
      url: 'https://example.com/post/1',
      platform: 'douyin',
    } satisfies ApiQuickTopPost,
  ],
  fallbackKols: [],
  degraded: false,
  pointsCost: 10,
  hasQueried: true,
};

// 当前 @testing-library/react 的 wrapper 不接收 renderHook 的 props，
// 用可变引用控制 Provider 的 userId 来模拟登录用户切换。
function setup(userId: string) {
  const userRef = { current: userId };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QuickFeatureCacheProvider userId={userRef.current}>{children}</QuickFeatureCacheProvider>
  );
  const hook = renderHook(() => useQuickFeatureCache(), { wrapper });
  return { ...hook, userRef };
}

describe('QuickFeatureCache', () => {
  it('同一用户写入爆贴状态后重新渲染消费者仍可读取', () => {
    const { result, rerender } = setup('user-a');

    act(() => {
      result.current.setTopPosts('douyin', douyinEntry);
    });
    rerender();

    expect(result.current.topPosts.douyin).toEqual(douyinEntry);
  });

  it('爆贴缓存按平台分键互不影响', () => {
    const { result } = setup('user-a');

    act(() => {
      result.current.setTopPosts('douyin', douyinEntry);
    });

    expect(result.current.topPosts.douyin).toEqual(douyinEntry);
    expect(result.current.topPosts.xiaohongshu).toBeUndefined();
  });

  it('切换用户后缓存回到初始状态', () => {
    const { result, rerender, userRef } = setup('user-a');

    act(() => {
      result.current.setTopPosts('douyin', douyinEntry);
      result.current.setKolRecommend({
        budget: 50_000,
        queriedBudget: 50_000,
        platforms: ['xiaohongshu', 'douyin'],
        queriedPlatforms: ['xiaohongshu', 'douyin'],
        items: [],
        pointsCost: 10,
        hasQueried: true,
      });
      result.current.setEvaluate({
        activityName: '七夕活动',
        kolNames: ['达人A'],
        kolDraft: '',
        result: null,
      });
    });

    userRef.current = 'user-b';
    rerender();

    expect(result.current.topPosts).toEqual({});
    expect(result.current.kolRecommend).toBeNull();
    expect(result.current.evaluate).toBeNull();
  });

  it('用户 A 的缓存不会在用户 B 登录时泄露，切回 A 也不保留', () => {
    const { result, rerender, userRef } = setup('user-a');

    act(() => {
      result.current.setTopPosts('douyin', douyinEntry);
    });

    userRef.current = 'user-b';
    rerender();
    expect(result.current.topPosts.douyin).toBeUndefined();

    userRef.current = 'user-a';
    rerender();
    expect(result.current.topPosts.douyin).toBeUndefined();
  });
});

describe('QuickFeatureCache · in-flight loading 状态', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('queryTopPosts 进行中 entry.loading 为 true 且保留旧字段，成功后复位', async () => {
    let resolveQuery: (value: ApiQuickTopPosts) => void = () => undefined;
    mockGetTopPosts.mockImplementation(
      () => new Promise(resolve => {
        resolveQuery = resolve;
      }),
    );
    const { result } = setup('user-a');

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.queryTopPosts('douyin');
    });

    // 查询中：loading=true，hasQueried 仍为 false（面板以 loading 分支优先展示）
    expect(result.current.topPosts.douyin).toMatchObject({
      loading: true,
      hasQueried: false,
      items: [],
    });

    await act(async () => {
      resolveQuery({ items: [], points_cost: 10 });
      await pending;
    });

    expect(result.current.topPosts.douyin).toMatchObject({
      loading: false,
      hasQueried: true,
      pointsCost: 10,
    });
  });

  it('queryTopPosts 失败同样复位 loading 并保留旧列表', async () => {
    mockGetTopPosts.mockRejectedValue(new Error('boom'));
    const { result } = setup('user-a');
    act(() => {
      result.current.setTopPosts('douyin', douyinEntry);
    });

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.queryTopPosts('douyin');
    });
    expect(result.current.topPosts.douyin).toMatchObject({ loading: true, hasQueried: true });

    await act(async () => {
      await pending;
    });
    expect(result.current.topPosts.douyin).toMatchObject({
      loading: false,
      hasQueried: true,
      error: '查询失败，请稍后重试',
      items: douyinEntry.items,
    });
  });

  it('queryKolRecommendations 进行中 entry.loading 为 true 且保留平台选择，settle 后复位', async () => {
    let resolveQuery: (value: { items: never[]; points_cost: number }) => void = () => undefined;
    mockGetKolRecommendations.mockImplementation(
      () => new Promise(resolve => {
        resolveQuery = resolve;
      }),
    );
    const { result } = setup('user-a');

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      result.current.setKolRecommend({
        budget: 50_000,
        queriedBudget: null,
        platforms: ['douyin', 'xiaohongshu'],
        queriedPlatforms: null,
        items: [],
        pointsCost: null,
        hasQueried: true,
      });
    });
    act(() => {
      pending = result.current.queryKolRecommendations(50_000, ['douyin', 'xiaohongshu']);
    });
    expect(result.current.kolRecommend).toMatchObject({
      loading: true,
      platforms: ['douyin', 'xiaohongshu'],
    });

    await act(async () => {
      resolveQuery({ items: [], points_cost: 20 });
      await pending;
    });
    expect(result.current.kolRecommend).toMatchObject({
      loading: false,
      queriedBudget: 50_000,
      hasQueried: true,
    });
  });
});

describe('QuickFeatureCache · 达人推荐 queriedBudget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('查询成功后回写 queriedBudget 为本次实际请求的预算', async () => {
    mockGetKolRecommendations.mockResolvedValue({ items: [], points_cost: 20 });
    const { result } = setup('user-a');

    await act(async () => {
      await result.current.queryKolRecommendations(120_000, ['xiaohongshu', 'douyin']);
    });

    expect(mockGetKolRecommendations).toHaveBeenCalledWith({
      budget: 120_000,
      platforms: ['xiaohongshu', 'douyin'],
    });
    expect(result.current.kolRecommend).toMatchObject({
      budget: 120_000,
      queriedBudget: 120_000,
      hasQueried: true,
    });
  });

  it('查询回写时记录本次实际请求的平台（queriedPlatforms 排序存储）', async () => {
    mockGetKolRecommendations.mockResolvedValue({ items: [], points_cost: 20 });
    const { result } = setup('user-a');
    act(() => {
      result.current.setKolRecommend({
        budget: 120_000,
        queriedBudget: null,
        // 缓存里的 platforms 保留用户的切换顺序；queriedPlatforms 要求排序存储
        platforms: ['douyin', 'xiaohongshu', 'bilibili'],
        queriedPlatforms: null,
        items: [],
        pointsCost: null,
        hasQueried: true,
      });
    });

    await act(async () => {
      await result.current.queryKolRecommendations(120_000, ['douyin', 'xiaohongshu', 'bilibili']);
    });

    expect(mockGetKolRecommendations).toHaveBeenCalledWith({
      budget: 120_000,
      platforms: ['douyin', 'xiaohongshu', 'bilibili'],
    });
    expect(result.current.kolRecommend).toMatchObject({
      platforms: ['douyin', 'xiaohongshu', 'bilibili'],
      queriedPlatforms: ['bilibili', 'douyin', 'xiaohongshu'],
    });
  });

  it('回写保留用户当前（in-flight 期间拖动过的）预算，只更新 queriedBudget', async () => {
    mockGetKolRecommendations.mockResolvedValue({ items: [], points_cost: 20 });
    const { result } = setup('user-a');
    // 模拟：已按 ¥5万 查过，用户把滑动条拖到 ¥30万（防抖查询 in-flight）
    act(() => {
      result.current.setKolRecommend({
        budget: 300_000,
        queriedBudget: 50_000,
        platforms: ['xiaohongshu'],
        queriedPlatforms: ['xiaohongshu'],
        items: [],
        pointsCost: null,
        hasQueried: true,
      });
    });

    await act(async () => {
      await result.current.queryKolRecommendations(120_000, ['xiaohongshu']);
    });

    expect(result.current.kolRecommend).toMatchObject({
      budget: 300_000,
      queriedBudget: 120_000,
      platforms: ['xiaohongshu'],
      queriedPlatforms: ['xiaohongshu'],
    });
  });

  it('查询失败同样记录 queriedBudget 与 queriedPlatforms，保留当前预算与旧列表只更新错误', async () => {
    mockGetKolRecommendations.mockRejectedValue(new Error('boom'));
    const { result } = setup('user-a');
    act(() => {
      result.current.setKolRecommend({
        budget: 300_000,
        queriedBudget: null,
        platforms: ['xiaohongshu', 'weibo'],
        queriedPlatforms: null,
        items: [],
        pointsCost: 20,
        hasQueried: true,
      });
    });

    await act(async () => {
      await result.current.queryKolRecommendations(120_000, ['xiaohongshu', 'weibo']);
    });

    expect(result.current.kolRecommend).toMatchObject({
      budget: 300_000,
      queriedBudget: 120_000,
      platforms: ['xiaohongshu', 'weibo'],
      queriedPlatforms: ['weibo', 'xiaohongshu'],
      pointsCost: 20,
      error: '查询失败，请稍后重试',
    });
  });
});
