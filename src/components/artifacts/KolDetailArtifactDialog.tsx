import { ExternalLink, Loader2, X } from 'lucide-react';
import { useEffect } from 'react';
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type { KolDetailPayload } from '../../api/agentArtifacts';
import { restrictedCount, restrictedRatio, restrictedScore } from '../reportPrimitives';

const PLATFORM_NAMES: Record<string, string> = {
  xiaohongshu: '小红书', douyin: '抖音', bilibili: 'B站', kuaishou: '快手', weibo: '微博',
};

function platformName(platform: string): string {
  return PLATFORM_NAMES[platform] ?? platform;
}

function Metric({ label, value }: { label: string; value: string }) {
  const isRestricted = value === '数据受限';
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2.5">
      <p className="text-[10px] font-medium text-slate-400">{label}</p>
      <p className={`mt-1 text-[15px] font-bold tracking-tight ${isRestricted ? 'text-slate-300' : 'text-slate-800'}`}>
        {value}
      </p>
    </div>
  );
}

function DistributionColumn({ title, items }: {
  title: string;
  items: KolDetailPayload['data']['audience']['gender_distribution'];
}) {
  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 p-3">
        <p className="text-[11px] font-semibold text-slate-600">{title}</p>
        <p className="mt-3 text-center text-[10px] text-slate-400">暂无数据</p>
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <p className="text-[11px] font-semibold text-slate-700">{title}</p>
      <ul className="mt-2 space-y-1">
        {items.map(item => (
          <li key={item.key} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
            <span className="min-w-0 truncate">{item.label}</span>
            <b className="shrink-0 text-slate-700">{restrictedRatio(item.share)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TrendChart({ trend }: { trend: KolDetailPayload['data']['trend'] }) {
  const rows = trend.map(item => ({ ...item, date: item.date.slice(5) }));
  return (
    <div className="h-48" aria-label="达人粉丝趋势图表">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 10, bottom: 0, left: -8 }}>
          <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} />
          <Tooltip formatter={(value) => [restrictedCount(Number(value)), '数值']} />
          <Line type="monotone" dataKey="followers" name="粉丝数" stroke="#4f46e5" strokeWidth={2} dot={{ r: 3 }} connectNulls />
          <Line type="monotone" dataKey="engagement" name="互动" stroke="#14b8a6" strokeWidth={2} dot={{ r: 3 }} connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function LatestPosts({ posts }: { posts: KolDetailPayload['data']['latest_posts'] }) {
  const list = posts.slice(0, 5);
  if (list.length === 0) return <p className="py-6 text-center text-[11px] text-slate-400">暂无热帖数据</p>;
  return (
    <div className="divide-y divide-slate-100">
      {list.map((post, index) => {
        const title = post.title || '无标题';
        const url = post.url;
        return (
          <article key={`${post.platform}-${post.post_id}-${index}`} className="flex items-center gap-3 py-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-indigo-50 text-[10px] font-bold text-indigo-600">{index + 1}</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[12px] font-semibold text-slate-700">
                {url ? (
                  <a aria-label={`查看热帖：${title}`} href={url} target="_blank" rel="noreferrer" className="hover:text-indigo-600 hover:underline">{title}</a>
                ) : title}
              </p>
              <p className="mt-1 text-[10px] text-slate-400">
                {url ? '' : '数据受限'}
                {post.published_at ? `${url ? ' · ' : ''}${post.published_at}` : ''}
              </p>
              <p className="mt-0.5 text-[10px] text-slate-400">
                赞 {restrictedScore(post.likes)} · 评 {restrictedScore(post.comments)} · 转 {restrictedScore(post.shares)}
              </p>
            </div>
            {url && <ExternalLink className="h-3.5 w-3.5 shrink-0 text-slate-300" aria-hidden="true" />}
          </article>
        );
      })}
    </div>
  );
}

export interface KolDetailArtifactDialogProps {
  payload?: KolDetailPayload;
  onClose: () => void;
}

export default function KolDetailArtifactDialog({ payload, onClose }: KolDetailArtifactDialogProps) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const identity = payload?.data.identity;
  const nickname = identity?.nickname || '达人详情';
  const homepageUrl = identity?.homepage_url;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/35 p-4"
      role="presentation"
      onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={`${nickname}达人详情`}
        className="flex max-h-[min(820px,calc(100vh-32px))] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-white/70 bg-slate-50 shadow-2xl"
      >
        <header className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-white px-5 py-3.5">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-sm font-bold text-white shadow-sm">
              {nickname.slice(0, 1)}
            </span>
            <div className="min-w-0">
              <h2 className="truncate text-[15px] font-bold text-slate-900">{nickname}</h2>
              {payload && (
                <p className="mt-0.5 text-[11px] text-slate-400">
                  达人详情 · {platformName(payload.scope.platform)}
                  {identity?.region ? ` · ${identity.region}` : ''}
                </p>
              )}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {payload && homepageUrl && (
              <a
                href={homepageUrl}
                target="_blank"
                rel="noreferrer"
                aria-label="打开主页"
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-[11px] font-semibold text-slate-600 hover:bg-slate-50"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                打开主页
              </a>
            )}
            <button
              type="button"
              aria-label="关闭达人详情"
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {!payload ? (
            <div role="status" className="flex min-h-72 flex-col items-center justify-center gap-3 text-[12px] text-slate-500">
              <Loader2 className="h-6 w-6 animate-spin text-indigo-500" />
              正在生成达人详情…
            </div>
          ) : (
            <div className="space-y-4">
              {payload.narrative.profile_summary && (
                <p className="rounded-lg bg-indigo-50/60 px-3 py-2 text-[11px] leading-4 text-indigo-700">
                  {payload.narrative.profile_summary}
                </p>
              )}

              {!homepageUrl && (
                <p className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
                  数据受限：该达人未提供主页链接
                </p>
              )}

              <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
                <Metric label="粉丝数" value={restrictedCount(payload.data.metrics.followers)} />
                <Metric label="关注" value={restrictedScore(payload.data.metrics.following)} />
                <Metric label="内容量" value={restrictedScore(payload.data.metrics.posts)} />
                <Metric label="获赞" value={restrictedScore(payload.data.metrics.likes)} />
                <Metric label="有效粉丝" value={restrictedCount(payload.data.metrics.active_followers)} />
                <Metric label="有效粉丝率" value={restrictedRatio(payload.data.metrics.active_follower_rate)} />
                <Metric label="粉丝增长" value={restrictedRatio(payload.data.metrics.growth_rate)} />
                <Metric label="总互动" value={restrictedScore(payload.data.metrics.engagement_total)} />
                <Metric label="均互动" value={restrictedScore(payload.data.metrics.avg_engagement)} />
              </div>

              <section>
                <h3 className="mb-2 text-[12px] font-bold text-slate-800">受众画像</h3>
                <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
                  <DistributionColumn title="性别" items={payload.data.audience.gender_distribution} />
                  <DistributionColumn title="年龄" items={payload.data.audience.age_distribution} />
                  <DistributionColumn title="地区" items={payload.data.audience.region_distribution} />
                  <DistributionColumn title="兴趣" items={payload.data.audience.interest_distribution} />
                </div>
              </section>

              <section className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                <h3 className="text-[12px] font-bold text-slate-800">粉丝趋势</h3>
                {payload.data.trend.length > 0
                  ? <div className="mt-3"><TrendChart trend={payload.data.trend} /></div>
                  : <p className="flex h-48 items-center justify-center text-[11px] text-slate-400">暂无趋势数据</p>}
              </section>

              <section className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                <div className="flex items-center justify-between">
                  <h3 className="text-[12px] font-bold text-slate-800">最新热帖</h3>
                  <span className="text-[10px] text-slate-400">最多展示 5 条</span>
                </div>
                <div className="mt-1">
                  <LatestPosts posts={payload.data.latest_posts} />
                </div>
              </section>

              {payload.narrative.content_strengths.length > 0 && (
                <section>
                  <h3 className="mb-2 text-[12px] font-bold text-slate-800">内容优势</h3>
                  <ul className="space-y-2">
                    {payload.narrative.content_strengths.map((item, index) => (
                      <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                        <p className="text-[12px] font-semibold text-slate-700">{item.title}</p>
                        {item.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{item.detail}</p>}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {payload.narrative.risk_notes.length > 0 && (
                <section>
                  <h3 className="mb-2 text-[12px] font-bold text-slate-800">风险提示</h3>
                  <ul className="space-y-2">
                    {payload.narrative.risk_notes.map((item, index) => (
                      <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                        <p className="text-[12px] font-semibold text-slate-700">{item.title}</p>
                        {item.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{item.detail}</p>}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
