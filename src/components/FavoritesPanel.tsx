import { RefreshCw, Star } from 'lucide-react';
import { Fragment, useEffect, useState } from 'react';

import { deleteFavorite, deleteFavoriteByKey } from '../api/favorites';
import type { ApiFavorite } from '../api/contracts';
import type { QuickKolSelection } from '../types';
import { formatExposure, formatNumber } from './reportPrimitives';

interface FavoritesPanelProps {
  favorites: readonly ApiFavorite[];
  loading?: boolean;
  onRefresh?: () => void;
  onCountChange?: (count: number) => void;
  onSelectKol?: (kol: QuickKolSelection) => void;
  /** 活跃会话 id；无会话时刷新入口显示「新建会话后刷新」，不回退旧 Quick API。 */
  sessionId?: string;
  /** 有活跃会话时的刷新入口：走新 kol-details API（createKolDetail）。 */
  onRefreshDetail?: (favorite: ApiFavorite) => void | Promise<unknown>;
}

function platformName(platform: string): string {
  return ({ xiaohongshu: '小红书', douyin: '抖音', bilibili: '哔哩哔哩', weibo: '微博', wechat: '微信' } as Record<string, string>)[platform] ?? platform;
}

function snapshotNumber(snapshot: Record<string, unknown> | null, key: string): number | null {
  const value = snapshot?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function snapshotText(snapshot: Record<string, unknown> | null, key: string): string {
  const value = snapshot?.[key];
  return typeof value === 'string' ? value.trim() : '';
}

// 快照报价：圈选卡片写 quoted_price_cny，快捷推荐卡片写 price。
function snapshotPrice(snapshot: Record<string, unknown> | null): number | null {
  return snapshotNumber(snapshot, 'quoted_price_cny') ?? snapshotNumber(snapshot, 'price');
}

interface FavoriteCardProps {
  favorite: ApiFavorite;
  selectable: boolean;
  onOpenDetail: () => void;
  onRemove: () => void;
  sessionId?: string;
  onRefreshDetail?: (favorite: ApiFavorite) => void | Promise<unknown>;
}

// 与圈选达人列表 KolSelectionCard 同款卡片：头像 + 昵称/星级 + 平台·地区·粉丝
// + 综合评分/rating 徽标 + 互动率/报价 chips；快照缺的字段直接省略。
function FavoriteCard({ favorite, selectable, onOpenDetail, onRemove, sessionId, onRefreshDetail }: FavoriteCardProps) {
  const name = favorite.nickname?.trim() || '未命名达人';
  const snapshot = favorite.snapshot;
  const stars = snapshotText(snapshot, 'stars');
  const rating = snapshotText(snapshot, 'rating');
  const total = snapshotNumber(snapshot, 'score_total');
  const followers = snapshotNumber(snapshot, 'followers');
  const city = snapshotText(snapshot, 'city');
  const engagementRate = snapshotNumber(snapshot, 'engagement_rate');
  const price = snapshotPrice(snapshot);
  const metaParts = [
    platformName(favorite.platform),
    city || null,
    followers !== null ? `粉丝 ${formatExposure(followers)}` : null,
  ].filter(Boolean);

  const head = (
    <>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[13px] font-bold text-indigo-600">
        {name.slice(0, 1)}
      </span>
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-1.5">
          <span className="truncate text-[12px] font-semibold text-slate-800">{name}</span>
          {stars && <span className="shrink-0 text-[10px] text-amber-500">{stars}</span>}
        </p>
        <p className="mt-0.5 truncate text-[10px] text-slate-400">{metaParts.join(' · ')}</p>
      </div>
      {(total !== null || rating) && (
        <div className="shrink-0 text-right">
          {total !== null && (
            <p className="text-[12px] font-bold text-slate-800">
              <span className="mr-1 text-[10px] font-normal text-slate-400">综合评分</span>{total}
            </p>
          )}
          {rating && (
            <span className="mt-1 inline-block rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-600">{rating}</span>
          )}
        </div>
      )}
    </>
  );

  return (
    <section className="rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm">
      <div className="flex items-center gap-2.5">
        {selectable ? (
          <button type="button" aria-label={`查看达人详情 ${name}`} onClick={onOpenDetail} className="flex min-w-0 flex-1 items-center gap-2.5 text-left">
            {head}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-2.5">{head}</div>
        )}
        <button type="button" aria-label={`取消收藏 ${name}`} onClick={onRemove} className="shrink-0 rounded-lg p-1.5 text-amber-500 transition hover:bg-amber-50">
          <Star className="h-3.5 w-3.5 fill-amber-400" />
        </button>
      </div>
      {(engagementRate !== null || price !== null) && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {engagementRate !== null && (
            <span className="rounded-lg bg-emerald-50 px-2 py-1 text-[10px] font-semibold text-emerald-600">互动率 {engagementRate}%</span>
          )}
          {price !== null && (
            <span className="rounded-lg bg-slate-100 px-2 py-1 text-[10px] font-semibold text-slate-600">预估报价 ¥{formatNumber(price)}</span>
          )}
        </div>
      )}
      {/* 达人详情刷新走新 kol-details API（Task 23 §13.3）：有活跃会话才可刷新，
          无会话提示新建会话后刷新，不回退旧 Quick API。 */}
      {selectable && (
        <div className="mt-2.5 border-t border-slate-100 pt-2">
          {sessionId && onRefreshDetail ? (
            <button
              type="button"
              aria-label={`刷新达人详情 ${name}`}
              onClick={() => void onRefreshDetail(favorite)}
              className="flex items-center gap-1 rounded-lg border border-indigo-100 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-600 transition hover:bg-indigo-100"
            >
              <RefreshCw className="h-3 w-3" aria-hidden="true" />
              刷新详情
            </button>
          ) : (
            <p className="text-[10px] font-medium text-slate-400">新建会话后刷新</p>
          )}
        </div>
      )}
    </section>
  );
}

export default function FavoritesPanel({ favorites, loading = false, onRefresh, onCountChange, onSelectKol, sessionId, onRefreshDetail }: FavoritesPanelProps) {
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
    const selectable = Boolean(onSelectKol && favorite.kol_uid);
    return (
      <Fragment key={favorite.id}>
        <FavoriteCard
          favorite={favorite}
          selectable={selectable}
          onOpenDetail={() => onSelectKol?.({ platform: favorite.platform, kw_uid: favorite.kol_uid!, nickname: name })}
          onRemove={() => void remove(favorite)}
          sessionId={sessionId}
          onRefreshDetail={onRefreshDetail}
        />
      </Fragment>
    );
  })}</div></div>;
}
