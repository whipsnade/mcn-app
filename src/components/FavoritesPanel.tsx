import { Star } from 'lucide-react';
import { useEffect, useState } from 'react';

import { deleteFavorite, deleteFavoriteByKey } from '../api/favorites';
import type { ApiFavorite } from '../api/contracts';
import { formatExposure } from './reportPrimitives';

interface FavoritesPanelProps {
  favorites: readonly ApiFavorite[];
  loading?: boolean;
  onRefresh?: () => void;
  onCountChange?: (count: number) => void;
}

function platformName(platform: string): string {
  return ({ xiaohongshu: '小红书', douyin: '抖音', bilibili: '哔哩哔哩', weibo: '微博', wechat: '微信' } as Record<string, string>)[platform] ?? platform;
}

function snapshotNumber(snapshot: Record<string, unknown> | null, key: string): number | null {
  const value = snapshot?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// 快照报价：圈选卡片写 quoted_price_cny，快捷推荐卡片写 price。
function snapshotPrice(snapshot: Record<string, unknown> | null): number | null {
  return snapshotNumber(snapshot, 'quoted_price_cny') ?? snapshotNumber(snapshot, 'price');
}

export default function FavoritesPanel({ favorites, loading = false, onRefresh, onCountChange }: FavoritesPanelProps) {
  const [error, setError] = useState<string>();

  useEffect(() => {
    onCountChange?.(favorites.length);
  }, [favorites.length, onCountChange]);

  const remove = async (favorite: ApiFavorite) => {
    setError(undefined);
    try {
      if (favorite.kol_uid) {
        await deleteFavoriteByKey(favorite.platform, favorite.kol_uid);
      } else if (favorite.kol_id) {
        await deleteFavorite(favorite.kol_id);
      } else {
        return;
      }
      onRefresh?.();
    } catch {
      setError('取消收藏失败，请稍后重试');
    }
  };

  if (loading && !favorites.length) return <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">正在加载收藏…</div>;
  if (!favorites.length) return <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">{error && <span role="alert" className="text-rose-500">{error}</span>}还没有收藏的达人</div>;

  return <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4"><div className="space-y-2">{error && <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>}{favorites.map(favorite => {
    const name = favorite.nickname?.trim() || '未命名达人';
    const followers = snapshotNumber(favorite.snapshot, 'followers');
    const price = snapshotPrice(favorite.snapshot);
    const metaParts = [
      platformName(favorite.platform),
      followers !== null ? `粉丝 ${formatExposure(followers)}` : null,
      price !== null ? `¥${price.toLocaleString('zh-CN')}` : null,
    ].filter(Boolean);
    return <div key={favorite.id} className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"><div><div className="text-xs font-semibold text-slate-800">{name}</div><div className="mt-1 text-[10px] text-slate-400">{metaParts.join(' · ')}</div></div><button type="button" aria-label={`取消收藏 ${name}`} onClick={() => void remove(favorite)} className="rounded-lg p-1.5 text-amber-500 transition hover:bg-amber-50"><Star className="h-3.5 w-3.5 fill-amber-400" /></button></div>;
  })}</div></div>;
}
