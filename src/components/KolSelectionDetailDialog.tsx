import { BarChart3, ExternalLink, Loader2, RefreshCw, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  Bar, BarChart, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';

import { getKolSelectionDetail, queryKolSelectionDetail } from '../api/kolSelection';
import type { KolSelectionDetail, KolSelectionItem } from '../api/kolSelection';
import { formatExposure, formatNumber } from './reportPrimitives';

interface KolSelectionDetailDialogProps {
  sessionId: string;
  setId?: string;
  item: KolSelectionItem;
  onClose: () => void;
}

const chartColors = ['#4f46e5', '#14b8a6', '#f59b00', '#ec4899', '#0ea5e9', '#8b5cf6'];

function numberOf(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function textOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function formatCount(value: unknown): string {
  const number = numberOf(value);
  return number == null ? '—' : formatExposure(number);
}

function formatPercent(value: unknown): string {
  const number = numberOf(value);
  return number == null ? '—' : `${number.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`;
}

function distribution(detail: Record<string, unknown>, key: string) {
  const value = detail[key];
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>)
    .map(([name, amount]) => ({ name, value: numberOf(amount) }))
    .filter((item): item is { name: string; value: number } => item.value !== null)
    .sort((a, b) => b.value - a.value)
    .slice(0, 5);
}

function DetailMetric({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2.5">
    <p className="text-[10px] font-medium text-slate-400">{label}</p>
    <p className={`mt-1 text-[15px] font-bold tracking-tight ${accent ? 'text-indigo-600' : 'text-slate-800'}`}>{value}</p>
  </div>;
}

function DistributionChart({ title, values }: { title: string; values: Array<{ name: string; value: number }> }) {
  if (values.length === 0) return <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 p-3">
    <p className="text-[11px] font-semibold text-slate-600">{title}</p>
    <p className="mt-4 text-center text-[10px] text-slate-400">暂无数据</p>
  </div>;
  return <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
    <p className="text-[11px] font-semibold text-slate-700">{title}</p>
    <div className="mt-1 h-28" aria-label={`${title}图表`}>
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie data={values} dataKey="value" nameKey="name" innerRadius={24} outerRadius={42} paddingAngle={2}>
            {values.map((entry, index) => <Cell key={entry.name} fill={chartColors[index % chartColors.length]} />)}
          </Pie>
          <Tooltip formatter={value => [`${formatNumber(Number(value))}%`, '占比']} />
        </PieChart>
      </ResponsiveContainer>
    </div>
    <div className="space-y-1">
      {values.map((entry, index) => <p key={entry.name} className="flex items-center justify-between gap-2 text-[10px] text-slate-500">
        <span className="min-w-0 truncate"><i className="mr-1 inline-block h-1.5 w-1.5 rounded-full" style={{ background: chartColors[index % chartColors.length] }} />{entry.name}</span>
        <span className="font-semibold text-slate-700">{entry.value}%</span>
      </p>)}
    </div>
  </div>;
}

function detailsError(reason: unknown): string {
  if (reason instanceof Error && reason.message === 'INSUFFICIENT_POINTS') return '积分不足，暂无法查询达人详情';
  return '达人详情加载失败，请稍后重试';
}

export default function KolSelectionDetailDialog({ sessionId, setId, item, onClose }: KolSelectionDetailDialogProps) {
  const [data, setData] = useState<KolSelectionDetail>();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let cancelled = false;
    const query = { set_id: setId, platform: item.platform, kol_uid: item.kol_uid };
    setLoading(true);
    setError(undefined);
    setData(undefined);
    getKolSelectionDetail(sessionId, query)
      .then(async cached => {
        if (cancelled) return;
        if (cached.source !== 'missing') {
          setData(cached);
          return;
        }
        const result = await queryKolSelectionDetail(sessionId, { ...query, refresh: false });
        if (!cancelled) setData(result);
      })
      .catch(reason => { if (!cancelled) setError(detailsError(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [item.kol_uid, item.platform, sessionId, setId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setError(undefined);
    try {
      setData(await queryKolSelectionDetail(sessionId, {
        set_id: setId, platform: item.platform, kol_uid: item.kol_uid, refresh: true,
      }));
    } catch (reason) {
      setError(detailsError(reason));
    } finally {
      setRefreshing(false);
    }
  };

  const detail = data?.detail ?? {};
  const posts = data?.posts ?? [];
  const trend = Array.isArray(detail.trend_points)
    ? detail.trend_points.map(point => point as Record<string, unknown>).filter(point => textOf(point.week_start) && numberOf(point.average_interactions) !== null)
    : [];
  const profileUrl = textOf(detail.profile_url) || item.profile_url || '';
  const score = detail.score && typeof detail.score === 'object' && !Array.isArray(detail.score)
    ? detail.score as Record<string, unknown> : item.score;
  const audienceAge = distribution(detail, 'audience_age');
  const audienceRegions = distribution(detail, 'audience_regions');
  const audienceInterests = distribution(detail, 'audience_interests');
  const nickname = textOf(detail.nickname) || item.nickname || '达人详情';

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/35 p-4" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section role="dialog" aria-modal="true" aria-label={`${nickname}达人详情`} className="flex max-h-[min(820px,calc(100vh-32px))] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-white/70 bg-slate-50 shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-white px-5 py-3.5">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-sm font-bold text-white shadow-sm">{nickname.slice(0, 1)}</span>
          <div className="min-w-0">
            <h2 className="truncate text-[15px] font-bold text-slate-900">{nickname}</h2>
            <p className="mt-0.5 text-[11px] text-slate-400">达人详情 BI · {item.platform === 'xiaohongshu' ? '小红书' : item.platform}</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {profileUrl && <a href={profileUrl} target="_blank" rel="noreferrer" aria-label="打开主页" className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-[11px] font-semibold text-slate-600 hover:bg-slate-50"><ExternalLink className="h-3.5 w-3.5" />打开主页</a>}
          <button type="button" aria-label="刷新达人详情" onClick={() => void refresh()} disabled={loading || refreshing} className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"><RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />刷新</button>
          <button type="button" aria-label="关闭达人详情" onClick={onClose} className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"><X className="h-4 w-4" /></button>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {loading ? <div role="status" className="flex min-h-72 flex-col items-center justify-center gap-3 text-[12px] text-slate-500"><Loader2 className="h-6 w-6 animate-spin text-indigo-500" />正在查询达人详情与热帖…</div>
          : error ? <div className="flex min-h-72 flex-col items-center justify-center gap-3 text-center"><p role="alert" className="text-[12px] font-medium text-rose-600">{error}</p><button type="button" onClick={() => void refresh()} className="rounded-lg border border-slate-200 px-3 py-1.5 text-[11px] font-semibold text-slate-600">重试查询</button></div>
          : <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-indigo-100 bg-indigo-50/70 px-3 py-2 text-[11px] text-indigo-700">
              <span>{data?.source === 'cache' ? '来自缓存，不消耗积分' : data?.source === 'refresh' ? `刷新完成，本次查询消耗 ${data.points_cost} 积分` : `本次查询消耗 ${data?.points_cost ?? 0} 积分`}</span>
              {data?.fetched_at && <span className="text-[10px] text-indigo-400">更新于 {new Date(data.fetched_at).toLocaleString('zh-CN')}</span>}
            </div>
            <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
              <DetailMetric label="粉丝数" value={formatCount(detail.followers ?? item.followers)} accent />
              <DetailMetric label="综合评分" value={numberOf(score.total)?.toLocaleString('zh-CN') ?? '—'} />
              <DetailMetric label="互动率" value={formatPercent(detail.engagement_rate ?? item.fields.engagement_rate)} />
              <DetailMetric label="有效粉丝率" value={formatPercent(detail.effective_follower_rate)} />
              <DetailMetric label="近30日均互动" value={formatCount(detail.recent_30d_average_interactions ?? detail.average_interactions)} />
            </div>
            <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
              <div className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                <div className="flex items-center gap-1.5"><BarChart3 className="h-4 w-4 text-indigo-600" /><h3 className="text-[12px] font-bold text-slate-800">互动趋势</h3></div>
                {trend.length > 0 ? <div className="mt-3 h-52" aria-label="互动趋势图表"><ResponsiveContainer width="100%" height="100%"><LineChart data={trend} margin={{ top: 8, right: 10, bottom: 0, left: -12 }}><XAxis dataKey="week_start" tickFormatter={value => String(value).slice(5)} tick={{ fontSize: 10, fill: '#94a3b8' }} /><YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} /><Tooltip formatter={value => [formatNumber(Number(value)), '平均互动']} labelFormatter={value => `周起始：${value}`} /><Line type="monotone" dataKey="average_interactions" stroke="#4f46e5" strokeWidth={2.5} dot={{ r: 3 }} /></LineChart></ResponsiveContainer></div> : <p className="flex h-52 items-center justify-center text-[11px] text-slate-400">暂无互动趋势数据</p>}
              </div>
              <div className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                <h3 className="text-[12px] font-bold text-slate-800">内容表现</h3>
                <div className="mt-3 h-52" aria-label="内容表现图表"><ResponsiveContainer width="100%" height="100%"><BarChart data={[
                  { name: '均互动', value: numberOf(detail.recent_30d_average_interactions ?? detail.average_interactions) ?? 0 },
                  { name: '活跃粉丝', value: numberOf(detail.active_follower_count) ?? 0 },
                  { name: '作品数', value: numberOf(detail.works_count) ?? 0 },
                ]} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}><XAxis dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} /><YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} /><Tooltip formatter={value => [formatNumber(Number(value)), '数值']} /><Bar dataKey="value" fill="#14b8a6" radius={[5, 5, 0, 0]} /></BarChart></ResponsiveContainer></div>
              </div>
            </div>
            <section><h3 className="mb-2 text-[12px] font-bold text-slate-800">受众画像</h3><div className="grid gap-3 md:grid-cols-3"><DistributionChart title="受众年龄" values={audienceAge} /><DistributionChart title="受众地区" values={audienceRegions} /><DistributionChart title="受众兴趣" values={audienceInterests} /></div></section>
            <section className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm"><div className="flex items-center justify-between"><h3 className="text-[12px] font-bold text-slate-800">最新热帖</h3><span className="text-[10px] text-slate-400">最多展示 5 条</span></div>{data?.posts_degraded ? <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">热帖服务暂不可用，已展示达人基础详情</p> : posts.length === 0 ? <p className="py-8 text-center text-[11px] text-slate-400">暂无热帖数据</p> : <div className="mt-2 divide-y divide-slate-100">{posts.slice(0, 5).map((post, index) => { const title = textOf(post.title) || '无标题'; const url = textOf(post.url); return <article key={`${url || title}-${index}`} className="flex items-center gap-3 py-3"><span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-indigo-50 text-[10px] font-bold text-indigo-600">{index + 1}</span><div className="min-w-0 flex-1"><p className="truncate text-[12px] font-semibold text-slate-700">{title}</p><p className="mt-1 text-[10px] text-slate-400">互动 {formatCount(post.interact)} · 点赞 {formatCount(post.like)} · 评论 {formatCount(post.comment)}{textOf(post.publish_time) ? ` · ${textOf(post.publish_time)}` : ''}</p></div>{url && <a aria-label={`查看热帖：${title}`} href={url} target="_blank" rel="noreferrer" className="shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-[10px] font-semibold text-indigo-600 hover:bg-indigo-50">查看</a>}</article>; })}</div>}</section>
          </div>}
      </div>
    </section>
  </div>;
}
