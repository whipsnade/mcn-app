import { RefreshCw, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';

import type { ApiQuickKolItem, ApiQuickPlatform, ApiQuickTopPost } from '../api/contracts';
import { getTopPosts, quickErrorMessage } from '../api/quick';
import { useLoadingMessage } from '../hooks/useLoadingMessage';
import { quickPlatformLabel } from './KolRecommendPanel';

interface TopPostsPanelProps {
  platform: ApiQuickPlatform;
}

function formatCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return value.toLocaleString('zh-CN');
}

function formatPublishTime(value: string | null): string {
  if (!value) return '—';
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return value;
  return new Date(timestamp).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

export default function TopPostsPanel({ platform }: TopPostsPanelProps) {
  const [items, setItems] = useState<ApiQuickTopPost[]>([]);
  const [fallbackKols, setFallbackKols] = useState<ApiQuickKolItem[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [pointsCost, setPointsCost] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  // 打开 tab 不自动查询：由「查询/刷新」按钮触发
  const [hasQueried, setHasQueried] = useState(false);
  const [queryNonce, setQueryNonce] = useState(0);
  const loadingMessage = useLoadingMessage(loading, [
    [0, '正在加载爆贴榜单…'],
    [8000, '数据服务响应较慢，请稍候…'],
    [25000, '仍在等待上游返回，请耐心稍候…'],
  ]);

  const handleQuery = () => {
    setHasQueried(true);
    setQueryNonce(nonce => nonce + 1);
  };

  useEffect(() => {
    if (!hasQueried) return;
    let current = true;
    setLoading(true);
    setError(undefined);
    getTopPosts(platform)
      .then(result => {
        if (!current) return;
        setItems(result.items ?? []);
        setFallbackKols(result.fallback_kols ?? []);
        setDegraded(result.degraded === true);
        setPointsCost(typeof result.points_cost === 'number' ? result.points_cost : null);
      })
      .catch((err: unknown) => {
        if (!current) return;
        setError(quickErrorMessage(err));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => { current = false; };
  }, [platform, queryNonce, hasQueried]);

  const title = platform === 'xiaohongshu' ? '小红书前十爆贴' : '抖音前十爆贴';

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-slate-50">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4">
        <h2 className="text-xs font-bold text-slate-800">
          {title}
          <span className="ml-2 text-[9px] font-medium text-slate-400">
            近 7 日 · 按互动数排序{pointsCost !== null ? ` · 消耗 ${pointsCost} 积分` : ''}
          </span>
        </h2>
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

      {!hasQueried ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">
          <RefreshCw className="h-8 w-8 text-slate-300 stroke-[1.5]" />
          点击右上角「查询/刷新」获取近 7 日爆贴榜单
        </div>
      ) : loading ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">
          {loadingMessage}
        </div>
      ) : error && items.length === 0 && fallbackKols.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-slate-50 text-xs font-medium text-slate-400">
          <span role="alert" className="text-rose-500">{error}</span>
          暂未获取到爆贴数据
        </div>
      ) : degraded && fallbackKols.length > 0 ? (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] font-medium text-amber-700">
            <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
            爆贴数据服务暂不可用，为你展示{quickPlatformLabel(platform)}同行业热门达人
          </div>
          <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
            <table className="w-full border-collapse text-[11px] text-slate-600">
              <thead>
                <tr className="bg-slate-50/80">
                  <th className="border-b border-slate-100 px-3 py-2 text-left font-semibold text-slate-500">达人</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">粉丝数</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">报价</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">互动率</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-left font-semibold text-slate-500">城市</th>
                </tr>
              </thead>
              <tbody>
                {fallbackKols.map((kol, index) => (
                  <tr key={`${kol.kw_uid}-${index}`} className="odd:bg-slate-50/60">
                    <td className="max-w-[180px] truncate px-3 py-2 align-top font-medium text-slate-700">
                      {kol.nickname || '—'}
                      <span className="ml-1.5 rounded bg-indigo-50 px-1 py-0.2 text-[9px] font-semibold text-indigo-600">
                        {quickPlatformLabel(kol.platform)}
                      </span>
                    </td>
                    <td className="px-2 py-2 text-right align-top">{formatCount(kol.fans)}</td>
                    <td className="px-2 py-2 text-right align-top">
                      {kol.price !== null ? `¥${formatCount(kol.price)}` : '无报价'}
                    </td>
                    <td className="px-2 py-2 text-right align-top">
                      {kol.engagement_rate !== null ? `${(kol.engagement_rate * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="px-2 py-2 align-top">{kol.city || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">
          近 7 日暂无{quickPlatformLabel(platform)}爆贴数据
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
            <table className="w-full border-collapse text-[11px] text-slate-600">
              <thead>
                <tr className="bg-slate-50/80">
                  <th className="border-b border-slate-100 px-3 py-2 text-left font-semibold text-slate-500">标题</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-left font-semibold text-slate-500">作者</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">互动数</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">点赞</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-right font-semibold text-slate-500">评论</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-left font-semibold text-slate-500">发布时间</th>
                  <th className="border-b border-slate-100 px-2 py-2 text-left font-semibold text-slate-500">链接</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => (
                  <tr key={`${item.url ?? item.title}-${index}`} className="odd:bg-slate-50/60">
                    <td className="max-w-[220px] truncate px-3 py-2 align-top font-medium text-slate-700" title={item.title}>
                      {item.title || '—'}
                    </td>
                    <td className="max-w-[100px] truncate px-2 py-2 align-top">{item.nickname || '—'}</td>
                    <td className="px-2 py-2 text-right align-top font-semibold text-slate-700">{formatCount(item.interact)}</td>
                    <td className="px-2 py-2 text-right align-top">{formatCount(item.like)}</td>
                    <td className="px-2 py-2 text-right align-top">{formatCount(item.comment)}</td>
                    <td className="whitespace-nowrap px-2 py-2 align-top">{formatPublishTime(item.publish_time)}</td>
                    <td className="px-2 py-2 align-top">
                      {item.url ? (
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-semibold text-indigo-600 hover:text-indigo-800 hover:underline"
                        >
                          查看
                        </a>
                      ) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
