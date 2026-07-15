import { Star } from 'lucide-react';
import { useEffect, useState } from 'react';

import { deleteFavorite, listFavorites } from '../api/favorites';
import type { ApiFavorite } from '../api/contracts';

interface FavoritesPanelProps {
  refreshKey: number;
  onCountChange?: (count: number) => void;
  onFavoritesChange?: (favorites: readonly ApiFavorite[]) => void;
}

export default function FavoritesPanel({ refreshKey, onCountChange, onFavoritesChange }: FavoritesPanelProps) {
  const [favorites, setFavorites] = useState<ApiFavorite[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(undefined);
    listFavorites().then(items => {
      if (!current) return;
      setFavorites(items);
      onCountChange?.(items.length);
      onFavoritesChange?.(items);
    }).catch(() => {
      if (current) {
        setError('收藏加载失败，请稍后重试');
      }
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [onCountChange, onFavoritesChange, refreshKey]);

  const remove = async (kolId: string) => {
    setError(undefined);
    try {
      await deleteFavorite(kolId);
      setFavorites(current => {
        const next = current.filter(item => item.kol_id !== kolId);
        onCountChange?.(next.length);
        onFavoritesChange?.(next);
        return next;
      });
    } catch {
      setError('取消收藏失败，请稍后重试');
    }
  };

  if (loading && !favorites.length) return <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">正在加载收藏…</div>;
  if (!favorites.length) return <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">{error && <span role="alert" className="text-rose-500">{error}</span>}还没有收藏的达人</div>;

  return <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4"><div className="space-y-2">{error && <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>}{favorites.map(favorite => <div key={favorite.kol_id} className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"><div><div className="text-xs font-semibold text-slate-800">{favorite.kol_id}</div><div className="mt-1 text-[10px] text-slate-400">{favorite.platform} · 跨会话收藏</div></div><button type="button" aria-label={`取消收藏 ${favorite.kol_id}`} onClick={() => void remove(favorite.kol_id)} className="rounded-lg p-1.5 text-amber-500 transition hover:bg-amber-50"><Star className="h-3.5 w-3.5 fill-amber-400" /></button></div>)}</div></div>;
}
