import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import type {
  ApiQuickEvaluateResult,
  ApiQuickKolItem,
  ApiQuickPlatform,
  ApiQuickTopPost,
} from '../api/contracts';

// 爆贴缓存按平台分键，切换 Tab 回来时可直接恢复上次查询结果。
export interface QuickTopPostsCacheEntry {
  items: ApiQuickTopPost[];
  fallbackKols: ApiQuickKolItem[];
  degraded: boolean;
  pointsCost: number | null;
  hasQueried: boolean;
  error?: string;
}

export interface QuickKolRecommendCacheEntry {
  budget: number;
  items: ApiQuickKolItem[];
  pointsCost: number | null;
  hasQueried: boolean;
  error?: string;
}

export interface QuickEvaluateCacheEntry {
  activityName: string;
  kolNames: string[];
  kolDraft: string;
  result: ApiQuickEvaluateResult | null;
  error?: string;
}

export interface QuickFeatureCacheState {
  topPosts: Partial<Record<ApiQuickPlatform, QuickTopPostsCacheEntry>>;
  kolRecommend: QuickKolRecommendCacheEntry | null;
  evaluate: QuickEvaluateCacheEntry | null;
}

export interface QuickFeatureCacheValue extends QuickFeatureCacheState {
  setTopPosts: (platform: ApiQuickPlatform, entry: QuickTopPostsCacheEntry) => void;
  setKolRecommend: (entry: QuickKolRecommendCacheEntry | null) => void;
  setEvaluate: (entry: QuickEvaluateCacheEntry | null) => void;
}

const EMPTY_STATE: QuickFeatureCacheState = {
  topPosts: {},
  kolRecommend: null,
  evaluate: null,
};

const QuickFeatureCacheContext = createContext<QuickFeatureCacheValue | null>(null);

export interface QuickFeatureCacheProviderProps {
  // userId 变化即重置缓存：不做跨用户持久化，避免数据泄露。
  userId: string;
  children: ReactNode;
}

export function QuickFeatureCacheProvider({ userId, children }: QuickFeatureCacheProviderProps) {
  const [cachedUserId, setCachedUserId] = useState(userId);
  const [state, setState] = useState<QuickFeatureCacheState>(EMPTY_STATE);

  // 渲染期间同步重置（React 推荐的 adjust-state-during-render 模式），
  // 保证切换用户的同一帧渲染里消费者读不到上一个用户的缓存。
  if (cachedUserId !== userId) {
    setCachedUserId(userId);
    setState(EMPTY_STATE);
  }

  const setTopPosts = useCallback(
    (platform: ApiQuickPlatform, entry: QuickTopPostsCacheEntry) => {
      setState(prev => ({ ...prev, topPosts: { ...prev.topPosts, [platform]: entry } }));
    },
    [],
  );

  const setKolRecommend = useCallback((entry: QuickKolRecommendCacheEntry | null) => {
    setState(prev => ({ ...prev, kolRecommend: entry }));
  }, []);

  const setEvaluate = useCallback((entry: QuickEvaluateCacheEntry | null) => {
    setState(prev => ({ ...prev, evaluate: entry }));
  }, []);

  const value = useMemo<QuickFeatureCacheValue>(
    () => ({
      topPosts: cachedUserId === userId ? state.topPosts : {},
      kolRecommend: cachedUserId === userId ? state.kolRecommend : null,
      evaluate: cachedUserId === userId ? state.evaluate : null,
      setTopPosts,
      setKolRecommend,
      setEvaluate,
    }),
    [cachedUserId, userId, state, setTopPosts, setKolRecommend, setEvaluate],
  );

  return (
    <QuickFeatureCacheContext.Provider value={value}>
      {children}
    </QuickFeatureCacheContext.Provider>
  );
}

export function useQuickFeatureCache(): QuickFeatureCacheValue {
  const ctx = useContext(QuickFeatureCacheContext);
  if (!ctx) {
    throw new Error('useQuickFeatureCache 必须在 QuickFeatureCacheProvider 内使用');
  }
  return ctx;
}
