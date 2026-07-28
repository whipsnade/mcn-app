import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiQuickTopPost } from '../api/contracts';
import { getKolRecommendations } from '../api/quick';
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

describe('QuickFeatureCache · 达人推荐 queriedBudget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('查询成功后回写 queriedBudget 为本次实际请求的预算', async () => {
    mockGetKolRecommendations.mockResolvedValue({ items: [], points_cost: 20 });
    const { result } = setup('user-a');

    await act(async () => {
      await result.current.queryKolRecommendations(120_000);
    });

    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 120_000 });
    expect(result.current.kolRecommend).toMatchObject({
      budget: 120_000,
      queriedBudget: 120_000,
      hasQueried: true,
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
        items: [],
        pointsCost: null,
        hasQueried: true,
      });
    });

    await act(async () => {
      await result.current.queryKolRecommendations(120_000);
    });

    expect(result.current.kolRecommend).toMatchObject({
      budget: 300_000,
      queriedBudget: 120_000,
    });
  });

  it('查询失败同样记录 queriedBudget，保留当前预算与旧列表只更新错误', async () => {
    mockGetKolRecommendations.mockRejectedValue(new Error('boom'));
    const { result } = setup('user-a');
    act(() => {
      result.current.setKolRecommend({
        budget: 300_000,
        queriedBudget: null,
        items: [],
        pointsCost: 20,
        hasQueried: true,
      });
    });

    await act(async () => {
      await result.current.queryKolRecommendations(120_000);
    });

    expect(result.current.kolRecommend).toMatchObject({
      budget: 300_000,
      queriedBudget: 120_000,
      pointsCost: 20,
      error: '查询失败，请稍后重试',
    });
  });
});
