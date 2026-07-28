import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import type {
  ApiQuickEvaluateResult,
  ApiQuickKolItem,
  ApiQuickPlatform,
  ApiQuickTopPost,
} from '../api/contracts';
import { getKolRecommendations, getTopPosts, quickErrorMessage } from '../api/quick';

// 爆贴缓存按平台分键，切换 Tab 回来时可直接恢复上次查询结果。
export interface QuickTopPostsCacheEntry {
  items: ApiQuickTopPost[];
  fallbackKols: ApiQuickKolItem[];
  degraded: boolean;
  pointsCost: number | null;
  hasQueried: boolean;
  // in-flight 标记：查询进行中为 true，面板卸载/重挂载后仍能恢复加载态并防止重复请求。
  loading?: boolean;
  error?: string;
}

export interface QuickKolRecommendCacheEntry {
  budget: number;
  // 本次实际成功/失败请求所用的预算；budget !== queriedBudget 说明缓存预算尚未查询
  // （典型场景：防抖期内切 Tab，防抖 timer 随卸载被清理，查询从未发出）。
  queriedBudget: number | null;
  // 用户当前选中的平台（chips 多选，默认全选）；queriedPlatforms 是本次实际请求的平台
  // 组合（排序存储），与 platforms 排序后不一致说明平台选择尚未查询，需补查。
  platforms: string[];
  queriedPlatforms: string[] | null;
  items: ApiQuickKolItem[];
  pointsCost: number | null;
  hasQueried: boolean;
  // in-flight 标记：查询进行中为 true，面板卸载/重挂载后仍能恢复加载态并防止重复请求。
  loading?: boolean;
  error?: string;
}

export interface QuickEvaluateCacheEntry {
  activityName: string;
  kolNames: string[];
  kolDraft: string;
  result: ApiQuickEvaluateResult | null;
  // in-flight 标记：提交进行中为 true，面板卸载/重挂载后仍能恢复提交中态并防止重复提交。
  submitting?: boolean;
  error?: string;
}

export interface QuickFeatureCacheState {
  topPosts: Partial<Record<ApiQuickPlatform, QuickTopPostsCacheEntry>>;
  kolRecommend: QuickKolRecommendCacheEntry | null;
  evaluate: QuickEvaluateCacheEntry | null;
}

export interface QuickFeatureCacheValue extends QuickFeatureCacheState {
  setTopPosts: (platform: ApiQuickPlatform, entry: QuickTopPostsCacheEntry) => void;
  // 查询爆贴并写入缓存；按平台递增序号抑制过期响应，reject 不会向上抛出。
  queryTopPosts: (platform: ApiQuickPlatform) => Promise<void>;
  setKolRecommend: (entry: QuickKolRecommendCacheEntry | null) => void;
  // 按预算与平台组合查询达人推荐并写入缓存；递增序号抑制过期响应，reject 不会向上抛出。
  queryKolRecommendations: (budget: number, platforms: string[]) => Promise<void>;
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
  // 每个平台独立的请求序号：回包时序号不匹配说明已有更新的查询，直接丢弃。
  const topPostsQuerySeq = useRef<Partial<Record<ApiQuickPlatform, number>>>({});
  // 达人推荐的请求序号：与爆贴同理，过期响应直接丢弃。
  const kolRecommendQuerySeq = useRef(0);
  const userIdRef = useRef(userId);
  userIdRef.current = userId;

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

  const queryTopPosts = useCallback(async (platform: ApiQuickPlatform) => {
    const seq = (topPostsQuerySeq.current[platform] ?? 0) + 1;
    topPostsQuerySeq.current[platform] = seq;
    const requestUserId = userIdRef.current;
    const isStale = () =>
      topPostsQuerySeq.current[platform] !== seq || userIdRef.current !== requestUserId;
    // 开始时把 in-flight 标记写入缓存（保留旧字段）：面板即使卸载重挂载也能恢复加载态，
    // 并据此阻止 in-flight 期间的重复查询。
    setState(prev => {
      const prevEntry = prev.topPosts[platform];
      return {
        ...prev,
        topPosts: {
          ...prev.topPosts,
          [platform]: {
            items: prevEntry?.items ?? [],
            fallbackKols: prevEntry?.fallbackKols ?? [],
            degraded: prevEntry?.degraded ?? false,
            pointsCost: prevEntry?.pointsCost ?? null,
            hasQueried: prevEntry?.hasQueried ?? false,
            error: prevEntry?.error,
            loading: true,
          },
        },
      };
    });
    try {
      const result = await getTopPosts(platform);
      if (isStale()) return;
      setState(prev => ({
        ...prev,
        topPosts: {
          ...prev.topPosts,
          [platform]: {
            items: result.items ?? [],
            fallbackKols: result.fallback_kols ?? [],
            degraded: result.degraded === true,
            pointsCost: typeof result.points_cost === 'number' ? result.points_cost : null,
            hasQueried: true,
            loading: false,
          },
        },
      }));
    } catch (error) {
      if (isStale()) return;
      const message = quickErrorMessage(error);
      // 刷新失败保留旧列表，只更新错误信息（与迁移前的面板行为一致）。
      setState(prev => {
        const prevEntry = prev.topPosts[platform];
        return {
          ...prev,
          topPosts: {
            ...prev.topPosts,
            [platform]: {
              items: prevEntry?.items ?? [],
              fallbackKols: prevEntry?.fallbackKols ?? [],
              degraded: prevEntry?.degraded ?? false,
              pointsCost: prevEntry?.pointsCost ?? null,
              hasQueried: true,
              loading: false,
              error: message,
            },
          },
        };
      });
    }
  }, []);

  const setKolRecommend = useCallback((entry: QuickKolRecommendCacheEntry | null) => {
    setState(prev => ({ ...prev, kolRecommend: entry }));
  }, []);

  const queryKolRecommendations = useCallback(async (budget: number, platforms: string[]) => {
    const seq = ++kolRecommendQuerySeq.current;
    const requestUserId = userIdRef.current;
    const isStale = () =>
      kolRecommendQuerySeq.current !== seq || userIdRef.current !== requestUserId;
    // 开始时把 in-flight 标记写入缓存（保留旧字段）：面板即使卸载重挂载也能恢复加载态，
    // 并据此阻止 in-flight 期间的重复查询。
    setState(prev => {
      const prevEntry = prev.kolRecommend;
      return {
        ...prev,
        kolRecommend: {
          budget: prevEntry?.budget ?? budget,
          queriedBudget: prevEntry?.queriedBudget ?? null,
          platforms: prevEntry?.platforms ?? platforms,
          queriedPlatforms: prevEntry?.queriedPlatforms ?? null,
          items: prevEntry?.items ?? [],
          pointsCost: prevEntry?.pointsCost ?? null,
          hasQueried: prevEntry?.hasQueried ?? false,
          error: prevEntry?.error,
          loading: true,
        },
      };
    });
    try {
      const result = await getKolRecommendations({ budget, platforms });
      if (isStale()) return;
      setState(prev => ({
        ...prev,
        kolRecommend: {
          // 回写保留用户当前预算与平台选择（in-flight 期间可能又拖过滑动条/切换 chips），
          // 只记录已查询的预算与平台组合（排序存储，避免 chips 切换顺序造成假差异）。
          budget: prev.kolRecommend?.budget ?? budget,
          queriedBudget: budget,
          platforms: prev.kolRecommend?.platforms ?? platforms,
          queriedPlatforms: [...platforms].sort(),
          items: result.items ?? [],
          pointsCost: typeof result.points_cost === 'number' ? result.points_cost : null,
          hasQueried: true,
          loading: false,
        },
      }));
    } catch (error) {
      if (isStale()) return;
      const message = quickErrorMessage(error);
      // 刷新失败保留旧列表与当前预算/平台选择，只更新已查询参数与错误信息（与迁移前的面板行为一致）。
      setState(prev => {
        const prevEntry = prev.kolRecommend;
        return {
          ...prev,
          kolRecommend: {
            budget: prevEntry?.budget ?? budget,
            queriedBudget: budget,
            platforms: prevEntry?.platforms ?? platforms,
            queriedPlatforms: [...platforms].sort(),
            items: prevEntry?.items ?? [],
            pointsCost: prevEntry?.pointsCost ?? null,
            hasQueried: true,
            loading: false,
            error: message,
          },
        };
      });
    }
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
      queryTopPosts,
      setKolRecommend,
      queryKolRecommendations,
      setEvaluate,
    }),
    [cachedUserId, userId, state, setTopPosts, queryTopPosts, setKolRecommend, queryKolRecommendations, setEvaluate],
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
