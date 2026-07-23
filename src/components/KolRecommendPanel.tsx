import { RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';

import type { ApiFavorite, ApiQuickKolItem } from '../api/contracts';
import { createFavoriteByKey, deleteFavoriteByKey } from '../api/favorites';
import { getKolRecommendations, quickErrorMessage } from '../api/quick';
import { useLoadingMessage } from '../hooks/useLoadingMessage';
import type { QuickKolSelection } from '../types';
import FavoriteStar from './FavoriteStar';

interface KolRecommendPanelProps {
  onSelectKol: (kol: QuickKolSelection) => void;
  favorites?: readonly ApiFavorite[];
  onFavoriteToggled?: () => void;
}

const MIN_BUDGET = 10_000;
const MAX_BUDGET = 500_000;
const BUDGET_STEP = 10_000;
const DEBOUNCE_MS = 800;

function formatBudget(budget: number): string {
  return `¥${(budget / 10_000).toFixed(1)}万`;
}

function formatCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return value.toLocaleString('zh-CN');
}

export function quickPlatformLabel(platform: string): string {
  return ({ xiaohongshu: '小红书', douyin: '抖音' } as Record<string, string>)[platform] ?? platform;
}

function platformBadgeClass(platform: string): string {
  if (platform === 'xiaohongshu') return 'bg-rose-50 text-rose-600 border border-rose-200/40';
  if (platform === 'douyin') return 'bg-slate-900 text-white border border-slate-900';
  return 'bg-indigo-50 text-indigo-700 border border-indigo-100';
}

export default function KolRecommendPanel({ onSelectKol, favorites = [], onFavoriteToggled }: KolRecommendPanelProps) {
  const [budget, setBudget] = useState(50_000);
  const [items, setItems] = useState<ApiQuickKolItem[]>([]);
  const [pointsCost, setPointsCost] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [favoriteBusyKey, setFavoriteBusyKey] = useState<string | null>(null);
  // 打开 tab 不自动查询：首次查询由「查询/刷新」按钮触发，之后拖动预算条继续防抖刷新
  const [hasQueried, setHasQueried] = useState(false);
  const [queryNonce, setQueryNonce] = useState(0);
  const loadingMessage = useLoadingMessage(loading, [
    [0, '正在加载达人推荐…'],
    [8000, '数据服务响应较慢，请稍候…'],
    [25000, '仍在等待上游返回，请耐心稍候…'],
  ]);

  const handleQuery = () => {
    setHasQueried(true);
    setQueryNonce(nonce => nonce + 1);
  };

  const isFavorited = (item: ApiQuickKolItem) =>
    favorites.some(favorite => favorite.platform === item.platform && favorite.kol_uid === item.kw_uid);

  // 快照防御取数：缺字段直接省略该键。
  const favoriteSnapshot = (item: ApiQuickKolItem): Record<string, unknown> => {
    const snapshot: Record<string, unknown> = {};
    if (item.fans !== null) snapshot.followers = item.fans;
    if (item.price !== null) snapshot.price = item.price;
    if (item.engagement_rate !== null) snapshot.engagement_rate = item.engagement_rate;
    if (item.city) snapshot.city = item.city;
    return snapshot;
  };

  const toggleFavorite = async (item: ApiQuickKolItem) => {
    const key = `${item.platform}-${item.kw_uid}`;
    if (favoriteBusyKey === key) return;
    setFavoriteBusyKey(key);
    try {
      if (isFavorited(item)) {
        await deleteFavoriteByKey(item.platform, item.kw_uid);
      } else {
        await createFavoriteByKey({
          platform: item.platform,
          kolUid: item.kw_uid,
          nickname: item.nickname || undefined,
          snapshot: favoriteSnapshot(item),
        });
      }
      onFavoriteToggled?.();
    } catch {
      setError('收藏操作失败，请稍后重试');
    } finally {
      setFavoriteBusyKey(current => (current === key ? null : current));
    }
  };

  // 预算滑动条 800ms 防抖刷新；过期响应经 current 标记丢弃
  useEffect(() => {
    if (!hasQueried) return;
    let current = true;
    setLoading(true);
    const timer = window.setTimeout(() => {
      getKolRecommendations({ budget })
        .then(result => {
          if (!current) return;
          setItems(result.items ?? []);
          setPointsCost(typeof result.points_cost === 'number' ? result.points_cost : null);
          setError(undefined);
        })
        .catch((err: unknown) => {
          if (!current) return;
          setError(quickErrorMessage(err));
        })
        .finally(() => {
          if (current) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [budget, queryNonce, hasQueried]);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-slate-50">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4">
        <h2 className="text-xs font-bold text-slate-800">预算内达人推荐</h2>
        <button
          type="button"
          onClick={handleQuery}
          disabled={loading}
          className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] font-bold text-indigo-600 transition hover:bg-indigo-50 active:scale-95 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          查询/刷新
        </button>
      </div>

      <div className="shrink-0 border-b border-slate-100 bg-white px-4 py-3">
        <div className="flex items-center justify-between text-[11px]">
          <span className="font-bold text-slate-500">单达人报价预算</span>
          <span className="font-bold text-indigo-600">{formatBudget(budget)}</span>
        </div>
        <input
          type="range"
          min={MIN_BUDGET}
          max={MAX_BUDGET}
          step={BUDGET_STEP}
          value={budget}
          onChange={event => setBudget(Number(event.target.value))}
          className="mt-2 w-full accent-indigo-600"
          aria-label="单达人报价预算"
        />
        <div className="mt-1 flex items-center justify-between text-[9px] font-medium text-slate-400">
          <span>¥1.0万</span>
          <span>每次刷新约 20 积分{pointsCost !== null ? ` · 上次消耗 ${pointsCost} 积分` : ''}</span>
          <span>¥50.0万</span>
        </div>
      </div>

      {!hasQueried ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">
          <RefreshCw className="h-8 w-8 text-slate-300 stroke-[1.5]" />
          拖动设置预算后，点击右上角「查询/刷新」获取达人推荐
        </div>
      ) : loading && items.length === 0 ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">
          {loadingMessage}
        </div>
      ) : !loading && error && items.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">
          <span role="alert" className="text-rose-500">{error}</span>
          暂未获取到达人推荐
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="space-y-2">
            {error && (
              <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>
            )}
            {items.length === 0 ? (
              <p className="py-8 text-center text-xs font-medium text-slate-400">当前预算下暂无符合条件的达人</p>
            ) : (
              items.map(item => (
                <div
                  key={`${item.platform}-${item.kw_uid}`}
                  className="flex items-start gap-1 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm transition hover:border-indigo-200 hover:bg-indigo-50/20"
                >
                  <button
                    type="button"
                    onClick={() => onSelectKol({ platform: item.platform, kw_uid: item.kw_uid, nickname: item.nickname })}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="truncate text-xs font-semibold text-slate-800">{item.nickname || '未命名达人'}</span>
                        <span className={`shrink-0 rounded px-1 py-0.2 text-[9px] font-semibold ${platformBadgeClass(item.platform)}`}>
                          {quickPlatformLabel(item.platform)}
                        </span>
                      </div>
                      <span className="shrink-0 text-[11px] font-bold text-indigo-600">
                        {item.price !== null ? `¥${item.price.toLocaleString('zh-CN')}` : '无报价'}
                      </span>
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-slate-400">
                      <span>粉丝 {formatCount(item.fans)}</span>
                      <span>互动率 {item.engagement_rate !== null ? `${item.engagement_rate}%` : '—'}</span>
                      {item.city && <span>{item.city}</span>}
                      {item.tags?.slice(0, 3).map(tag => <span key={tag}>#{tag}</span>)}
                    </div>
                  </button>
                  <FavoriteStar
                    active={isFavorited(item)}
                    busy={favoriteBusyKey === `${item.platform}-${item.kw_uid}`}
                    onToggle={() => void toggleFavorite(item)}
                  />
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
