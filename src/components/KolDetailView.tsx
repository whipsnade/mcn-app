import { X } from 'lucide-react';
import { useEffect, useState } from 'react';

import type { ApiQuickKolDetail } from '../api/contracts';
import { getKolDetail, quickErrorMessage } from '../api/quick';
import type { QuickKolSelection } from '../types';
import { quickPlatformLabel } from './KolRecommendPanel';

interface KolDetailViewProps {
  selection: QuickKolSelection;
  onClose: () => void;
}

function pickNumber(source: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function pickText(source: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function formatCount(value: number | null): string {
  if (value === null) return '—';
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return value.toLocaleString('zh-CN');
}

// detail/posts 结构宽松：对常见字段名变体逐个兜底
function audienceSummary(detail: Record<string, unknown>): string {
  const direct = pickText(detail, ['audience_summary', 'audience', 'fans_audience', 'audience_profile']);
  if (direct) return direct;
  const nested = ['audience', 'fans_audience', 'audience_profile']
    .map(key => detail[key])
    .find((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value));
  if (!nested) return '';
  const parts = [
    pickText(nested, ['gender', 'majority_gender', 'gender_distribution']),
    pickText(nested, ['age', 'age_range', 'majority_age', 'age_distribution']),
    pickText(nested, ['region', 'city', 'province', 'majority_region']),
  ].filter(Boolean);
  return parts.join(' · ');
}

function postText(post: Record<string, unknown>, keys: string[]): string {
  return pickText(post, keys);
}

function postNumber(post: Record<string, unknown>, keys: string[]): number | null {
  return pickNumber(post, keys);
}

export default function KolDetailView({ selection, onClose }: KolDetailViewProps) {
  const [data, setData] = useState<ApiQuickKolDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(undefined);
    setData(null);
    getKolDetail(selection)
      .then(result => {
        if (!current) return;
        setData(result);
      })
      .catch((err: unknown) => {
        if (!current) return;
        setError(quickErrorMessage(err));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => { current = false; };
  }, [selection]);

  const detail = data?.detail ?? {};
  const posts = Array.isArray(data?.posts) ? data.posts : [];
  const postsDegraded = data?.posts_degraded === true;
  const fans = pickNumber(detail, ['fans', 'fans_count', 'followers', 'follower_count']);
  const price = pickNumber(detail, ['price', 'quote', 'unit_price']);
  const engagementRate = pickNumber(detail, ['engagement_rate', 'interaction_rate']);
  const audience = audienceSummary(detail);

  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4">
        <div className="min-w-0">
          <h2 className="truncate text-xs font-bold uppercase tracking-widest text-slate-800">
            {selection.nickname || '达人详情'}
          </h2>
          <p className="mt-0.5 text-[9px] text-slate-400">{quickPlatformLabel(selection.platform)} · 达人详情与热帖</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭达人详情"
          title="关闭"
          className="ml-2 shrink-0 rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50/40 p-8 text-center text-xs text-slate-500">
          正在加载达人详情…
        </div>
      ) : error ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50/40 p-8 text-center text-xs">
          <span role="alert" className="font-medium text-rose-500">{error}</span>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto bg-slate-50/40 p-3">
          <div className="space-y-3">
            <section className="rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm">
              <div className="grid grid-cols-3 gap-2 text-center">
                <div>
                  <p className="text-[10px] font-medium text-slate-400">粉丝</p>
                  <p className="mt-1 text-sm font-bold text-slate-800">{formatCount(fans)}</p>
                </div>
                <div>
                  <p className="text-[10px] font-medium text-slate-400">报价</p>
                  <p className="mt-1 text-sm font-bold text-slate-800">
                    {price !== null ? `¥${price.toLocaleString('zh-CN')}` : '—'}
                  </p>
                </div>
                <div>
                  <p className="text-[10px] font-medium text-slate-400">互动率</p>
                  <p className="mt-1 text-sm font-bold text-slate-800">
                    {engagementRate !== null ? `${engagementRate}%` : '—'}
                  </p>
                </div>
              </div>
              {audience && (
                <p className="mt-3 border-t border-slate-50 pt-2.5 text-[10px] leading-4 text-slate-500">
                  <span className="font-semibold text-slate-600">受众画像：</span>{audience}
                </p>
              )}
            </section>

            <section className="space-y-2">
              <h3 className="px-1 text-[11px] font-bold text-slate-500">最近 10 条热帖</h3>
              {postsDegraded ? (
                <p className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700">
                  热帖数据服务暂不可用，仅展示达人详情
                </p>
              ) : posts.length === 0 ? (
                <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-400">暂无热帖数据</p>
              ) : (
                posts.map((post, index) => {
                  const title = postText(post, ['title', 'content', 'desc']) || '无标题';
                  const interact = postNumber(post, ['interact', 'interaction', 'interact_count']);
                  const like = postNumber(post, ['like', 'likes', 'like_count']);
                  const comment = postNumber(post, ['comment', 'comments', 'comment_count']);
                  const publishTime = postText(post, ['publish_time', 'published_at', 'create_time']);
                  const url = postText(post, ['url', 'link', 'note_url']);
                  return (
                    <article key={`${url || title}-${index}`} className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
                      <div className="flex items-start justify-between gap-2">
                        <p className="min-w-0 text-[11px] font-semibold leading-4 text-slate-700">{title}</p>
                        {url && (
                          <a
                            href={url}
                            target="_blank"
                            rel="noreferrer"
                            className="shrink-0 text-[10px] font-semibold text-indigo-600 hover:text-indigo-800 hover:underline"
                          >
                            查看
                          </a>
                        )}
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-slate-400">
                        <span>互动 {formatCount(interact)}</span>
                        <span>点赞 {formatCount(like)}</span>
                        <span>评论 {formatCount(comment)}</span>
                        {publishTime && <span>{publishTime}</span>}
                      </div>
                    </article>
                  );
                })
              )}
            </section>
          </div>
        </div>
      )}
    </aside>
  );
}
