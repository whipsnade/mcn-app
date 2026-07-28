import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import type { ApiQuickTopPost } from '../api/contracts';
import {
  QuickFeatureCacheProvider,
  useQuickFeatureCache,
  type QuickTopPostsCacheEntry,
} from './QuickFeatureCache';

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
