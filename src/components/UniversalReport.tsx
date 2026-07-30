import {
  Activity, BarChart2, Database, Loader2, PieChart as PieChartIcon, Sparkles, Table as TableIcon, Tags,
} from 'lucide-react';
import {
  Bar, BarChart, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Fragment, useEffect, useRef, useState } from 'react';

import { downloadKolSelection, getKolSelection, getKolTop10Trend, listSelectionSets, runKolAnalysis } from '../api/kolSelection';
import type { KolSelectionItem, KolTop10TrendItem } from '../api/kolSelection';
import { listSessionReports } from '../api/reports';
import type { SessionReportType } from '../api/reports';
import { getAnalysisReport } from '../api/tasks';
import type {
  ApiAnalysisReport, ApiAnalysisReportChartSeries, ApiAnalysisReportMetricItem, ApiArtifactsSummary,
  ApiArtifactModuleSummary, ApiFavorite, ApiSelectionSetItem, ApiSessionReportItem, ApiTaskStatus,
  ArtifactModuleKey, ReportBlock,
} from '../api/contracts';
import { createFavoriteByKey, deleteFavoriteByKey } from '../api/favorites';
import FavoriteStar from './FavoriteStar';
import KolSelectionDetailDialog from './KolSelectionDetailDialog';
import { Card, formatExposure, formatNumber, MetricCard } from './reportPrimitives';
import { useLoadingMessage } from '../hooks/useLoadingMessage';

interface UniversalReportProps {
  report?: ApiAnalysisReport;
  taskStatus?: ApiTaskStatus | string;
  sessionId?: string;
  selectionCount?: number;
  onReportReady?: (report: ApiAnalysisReport) => void;
  favorites?: readonly ApiFavorite[];
  onFavoriteToggled?: () => void;
  artifactsSummary?: ApiArtifactsSummary;
  onMarkArtifactSeen?: (moduleKey: ArtifactModuleKey, artifactId: string) => void;
}

const chartColors = ['#4f46e5', '#818cf8', '#14b8a6', '#f59b00', '#0ea5e9', '#f43f4f'];
const trendColors = [...chartColors, '#8b5cf6', '#ec4899', '#22c55e', '#64748b'];

function Top10KolTrendChart({ items }: { items: KolTop10TrendItem[] }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const weeks = [...new Set(items.flatMap(item => item.trend_points.map(point => point.week_start)))].sort();
  if (!weeks.length) return null;
  const names = items.map(item => `#${item.rank} ${item.nickname || item.kol_uid}`);
  const data = weeks.map(week => {
    const row: Record<string, string | number> = { week: week.slice(5) };
    items.forEach((item, index) => {
      const point = item.trend_points.find(value => value.week_start === week);
      if (point) row[names[index]] = point.average_interactions;
    });
    return row;
  });
  return <Card title="Top10 KOL互动趋势" icon={<Activity className="h-4 w-4" />}>
    <p className="mb-2 text-[10px] text-slate-400">近四周平均单帖互动量 · 点击图例筛选达人</p>
    <div className="mb-2 flex flex-wrap gap-1" aria-label="KOL趋势图例">
      {names.map((name, index) => <button key={name} type="button" onClick={() => setHidden(current => { const next = new Set(current); next.has(name) ? next.delete(name) : next.add(name); return next; })} className={`rounded px-1.5 py-0.5 text-[9px] ${hidden.has(name) ? 'bg-slate-100 text-slate-400 line-through' : 'bg-indigo-50 text-slate-600'}`}><i className="mr-1 inline-block h-1.5 w-1.5 rounded-full" style={{ backgroundColor: trendColors[index] }} />{name}</button>)}
    </div>
    <div className="h-48" aria-label="Top10 KOL互动趋势图表"><ResponsiveContainer width="100%" height="100%"><LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}><XAxis dataKey="week" tick={{ fontSize: 10, fill: '#94a3b8' }} /><YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} /><Tooltip formatter={(value, name) => [formatNumber(Number(value)), name]} />{names.map((name, index) => !hidden.has(name) && <Line key={name} type="monotone" dataKey={name} stroke={trendColors[index]} strokeWidth={2} dot={{ r: 2 }} connectNulls />)}</LineChart></ResponsiveContainer></div>
  </Card>;
}

function isTerminal(status?: string): boolean {
  return status === 'completed' || status === 'completed_with_warnings' || status === 'insufficient_balance';
}

// 手动分析的失败文案：409 无圈选 / 同 tick 双击引发的版本冲突单独提示。
function analyzeErrorMessage(reason: unknown): string {
  if (reason instanceof Error) {
    if (reason.message === 'NO_KOL_SELECTION') return '暂无圈选达人，请先在会话中完成圈选';
    if (reason.message === 'REPORT_VERSION_CONFLICT') return '报告生成中，请稍后刷新查看';
  }
  return '分析失败，请稍后重试';
}

function textOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function HeadingBlock({ block }: { block: Extract<ReportBlock, { type: 'heading' }> }) {
  const text = textOf(block.text);
  if (!text) return null;
  return <h2 className="px-1 pt-1 text-[13px] font-bold text-slate-800">{text}</h2>;
}

export function MarkdownBlock({ block }: { block: Extract<ReportBlock, { type: 'markdown' }> }) {
  const text = typeof block.text === 'string' ? block.text : '';
  if (!text.trim()) return null;
  return (
    <p className="whitespace-pre-wrap rounded-xl border border-slate-100 bg-white p-3.5 text-[12px] leading-5 text-slate-600 shadow-sm">{text}</p>
  );
}

function metricValueText(value: string | number): string {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }
  return String(value);
}

// 字符串取值无法走 MetricCard 的指标结构时，使用与 MetricCard 一致的卡片样式渲染。
function StringMetricCard({ item }: { item: ApiAnalysisReportMetricItem }) {
  const delta = textOf(item.delta);
  return (
    <section className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3.5 shadow-sm">
      <div className="flex items-center gap-1 text-[12px] font-medium text-slate-400">
        <span className="truncate">{item.label}</span>
      </div>
      <p className="mt-2 truncate text-[24px] font-bold leading-none tracking-tight text-slate-800">{metricValueText(item.value)}</p>
      <div className="mt-1 flex items-center gap-1.5 text-[10px] text-slate-400">
        {item.unit && <span>{item.unit}</span>}
        {delta && <span className={delta.startsWith('-') ? 'text-rose-500' : 'text-emerald-600'}>{delta}</span>}
      </div>
    </section>
  );
}

function MetricGridBlock({ block }: { block: Extract<ReportBlock, { type: 'metric_grid' }> }) {
  const items = (block.items ?? []).filter(item => textOf(item.label));
  if (items.length === 0) return null;
  return (
    <Card title={textOf(block.title) || '核心指标'} icon={<BarChart2 className="h-4 w-4" />}>
      <div className="grid grid-cols-2 gap-2.5">
        {items.map((item, index) => (
          <div key={`${item.label}-${index}`}>
            {typeof item.value === 'number' && Number.isFinite(item.value) ? (
              <div className="space-y-1">
                <MetricCard label={item.label} metric={{ value: item.value, unit: item.unit ?? '', available: true, coverage: 1, source_fields: [], platforms: [] }} />
                {textOf(item.delta) && (
                  <p className={`px-1 text-[10px] ${textOf(item.delta).startsWith('-') ? 'text-rose-500' : 'text-emerald-600'}`}>{item.delta}</p>
                )}
              </div>
            ) : (
              <StringMetricCard item={item} />
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

function TableBlock({ block }: { block: Extract<ReportBlock, { type: 'table' }> }) {
  const columns = (block.columns ?? []).filter(column => textOf(column));
  const rows = (block.rows ?? []).filter(row => Array.isArray(row) && row.length > 0);
  if (columns.length === 0 || rows.length === 0) return null;
  return (
    <Card title={textOf(block.title) || '数据明细'} icon={<TableIcon className="h-4 w-4" />}>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[11px] text-slate-600">
          <thead>
            <tr>
              {columns.map(column => (
                <th key={column} className="border-b border-slate-100 px-2 py-1.5 text-left font-semibold text-slate-500">{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="odd:bg-slate-50/60">
                {columns.map((_, columnIndex) => (
                  <td key={columnIndex} className="px-2 py-1.5 align-top">{row[columnIndex] ?? '—'}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function toChartRows(categories: string[], series: ApiAnalysisReportChartSeries[]) {
  return categories.map((name, index) => {
    const row: Record<string, string | number | null> = { name };
    for (const item of series) row[item.name] = item.values[index] ?? null;
    return row;
  });
}

function validSeries(series: ApiAnalysisReportChartSeries[] | undefined): ApiAnalysisReportChartSeries[] {
  return (series ?? []).filter(item => textOf(item.name) && Array.isArray(item.values) && item.values.some(value => typeof value === 'number' && Number.isFinite(value)));
}

function BarChartBlock({ block }: { block: Extract<ReportBlock, { type: 'bar_chart' }> }) {
  const series = validSeries(block.series);
  const categories = (block.categories ?? []).filter(category => textOf(category));
  if (categories.length === 0 || series.length === 0) return null;
  const data = toChartRows(categories, series);
  return (
    <Card title={textOf(block.title) || '柱状对比'} icon={<BarChart2 className="h-4 w-4" />}>
      <div className="h-44" aria-label={`${textOf(block.title) || '柱状对比'}图表`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
            <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} width={42} />
            <Tooltip formatter={(value) => [formatNumber(Number(value)), '数值']} />
            {series.length > 1 && <Legend wrapperStyle={{ fontSize: 10 }} />}
            {series.map((item, index) => (
              <Bar key={item.name} dataKey={item.name} fill={chartColors[index % chartColors.length]} radius={[4, 4, 0, 0]} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

function LineChartBlock({ block }: { block: Extract<ReportBlock, { type: 'line_chart' }> }) {
  const series = validSeries(block.series);
  const categories = (block.categories ?? []).filter(category => textOf(category));
  if (categories.length < 2 || series.length === 0) return null;
  const data = toChartRows(categories, series);
  return (
    <Card title={textOf(block.title) || '趋势变化'} icon={<Activity className="h-4 w-4" />}>
      <div className="h-44" aria-label={`${textOf(block.title) || '趋势变化'}图表`}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
            <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} width={42} />
            <Tooltip formatter={(value) => [formatNumber(Number(value)), '数值']} />
            {series.length > 1 && <Legend wrapperStyle={{ fontSize: 10 }} />}
            {series.map((item, index) => (
              <Line
                key={item.name}
                type="monotone"
                dataKey={item.name}
                stroke={chartColors[index % chartColors.length]}
                strokeWidth={2}
                dot={{ r: 3, fill: chartColors[index % chartColors.length], strokeWidth: 0 }}
                activeDot={{ r: 5 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

function PieChartBlock({ block }: { block: Extract<ReportBlock, { type: 'pie_chart' }> }) {
  const series = validSeries(block.series);
  const first = series[0];
  const categories = (block.categories ?? []).filter(category => textOf(category));
  if (!first || categories.length === 0) return null;
  const data = categories.flatMap((name, index) => {
    const value = first.values[index];
    return typeof value === 'number' && Number.isFinite(value) ? [{ name, value }] : [];
  });
  if (data.length === 0) return null;
  return (
    <Card title={textOf(block.title) || '占比分布'} icon={<PieChartIcon className="h-4 w-4" />}>
      <div className="flex items-center gap-2">
        <div className="h-[130px] w-[130px] shrink-0" aria-label={`${textOf(block.title) || '占比分布'}环形图`}>
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={data} dataKey="value" nameKey="name" innerRadius={34} outerRadius={57} paddingAngle={2} stroke="none">
                {data.map((item, index) => <Cell key={item.name} fill={chartColors[index % chartColors.length]} />)}
              </Pie>
              <Tooltip formatter={(value) => [formatNumber(Number(value)), first.name]} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <div className="min-w-0 flex-1 space-y-1.5">
          {data.map((item, index) => (
            <div key={item.name} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
              <span className="flex min-w-0 items-center gap-1.5 truncate">
                <i className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: chartColors[index % chartColors.length] }} />{item.name}
              </span>
              <b className="text-slate-700">{formatNumber(item.value)}</b>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

function TagListBlock({ block }: { block: Extract<ReportBlock, { type: 'tag_list' }> }) {
  const items = (block.items ?? []).filter(item => textOf(item));
  if (items.length === 0) return null;
  return (
    <Card title={textOf(block.title) || '关键词'} icon={<Tags className="h-4 w-4" />}>
      <div className="flex flex-wrap gap-2">
        {items.map((item, index) => (
          <span key={`${item}-${index}`} className={`rounded-lg px-2.5 py-1.5 text-[11px] font-semibold ${index % 3 === 0 ? 'bg-emerald-50 text-emerald-600' : index % 3 === 1 ? 'bg-slate-100 text-slate-500' : 'bg-indigo-50 text-indigo-600'}`}>
            {item}
          </span>
        ))}
      </div>
    </Card>
  );
}

function SourcesBlock({ block }: { block: Extract<ReportBlock, { type: 'sources' }> }) {
  const items = (block.items ?? []).filter(item => textOf(item.name));
  if (items.length === 0) return null;
  return (
    <Card title="数据来源" icon={<Database className="h-4 w-4" />}>
      <ul className="space-y-2">
        {items.map((item, index) => (
          <li key={`${item.name}-${index}`} className="rounded-lg bg-slate-50 px-2.5 py-2 text-[10px] text-slate-500">
            <b className="block text-slate-700">{item.name}</b>
            <span>
              {item.collected_at ? `采集时间：${item.collected_at}` : '采集时间未标注'}
              {item.evidence ? ` · 证据编号：${item.evidence}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function blockHasContent(block: ReportBlock): boolean {
  switch (block.type) {
    case 'heading': return Boolean(textOf(block.text));
    case 'markdown': return typeof block.text === 'string' && Boolean(block.text.trim());
    case 'metric_grid': return (block.items ?? []).some(item => textOf(item.label));
    case 'table':
      return (block.columns ?? []).some(column => textOf(column))
        && (block.rows ?? []).some(row => Array.isArray(row) && row.length > 0);
    case 'bar_chart':
      return (block.categories ?? []).some(category => textOf(category)) && validSeries(block.series).length > 0;
    case 'line_chart':
      return (block.categories ?? []).filter(category => textOf(category)).length >= 2
        && validSeries(block.series).length > 0;
    case 'pie_chart': {
      const first = validSeries(block.series)[0];
      if (!first) return false;
      return (block.categories ?? []).some(
        (name, index) => Boolean(textOf(name))
          && typeof first.values[index] === 'number'
          && Number.isFinite(first.values[index]),
      );
    }
    case 'tag_list': return (block.items ?? []).some(item => textOf(item));
    case 'sources': return (block.items ?? []).some(item => textOf(item.name));
    default: return false;
  }
}

function ReportBlockView({ block }: { block: ReportBlock }) {
  switch (block.type) {
    case 'heading': return <HeadingBlock block={block} />;
    case 'markdown': return <MarkdownBlock block={block} />;
    case 'metric_grid': return <MetricGridBlock block={block} />;
    case 'table': return <TableBlock block={block} />;
    case 'bar_chart': return <BarChartBlock block={block} />;
    case 'line_chart': return <LineChartBlock block={block} />;
    case 'pie_chart': return <PieChartBlock block={block} />;
    case 'tag_list': return <TagListBlock block={block} />;
    case 'sources': return <SourcesBlock block={block} />;
    default: return null;
  }
}

// ---- 圈选达人列表 ----

const kolPlatformNames: Record<string, string> = {
  xiaohongshu: '小红书',
  douyin: '抖音',
  bilibili: 'B站',
  kuaishou: '快手',
  weibo: '微博',
};

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

const SCORE_GUIDE = `评分 v2（八维加权，缺失/无效/无法匹配该维记 0 分，不做估算）：
行业兴趣 10%（受众兴趣与行业匹配占比）｜目标地区 8%（受众地区与目标地区匹配占比）｜目标年龄 8%（受众年龄桶与目标年龄相交占比）｜互动表现 20%（近 30 天平均单帖互动量，分档）｜活跃粉丝 15%（有效粉丝率或活跃粉丝/粉丝数）｜内容质量 15%（供应商综合评分）｜粉丝规模 10%（粉丝数，分档）｜互动粉丝比 14%（同一平均互动量/粉丝数，分档）。
分档：粉丝数 <1万/1-10万/10-50万/50-100万/≥100万 对应 20/40/60/80/100 分；平均互动 <1千/1千-5千/5千-2万/2万-10万/≥10万 同档；互动粉丝比 <0.5%/0.5-1%/1-3%/3-6%/≥6% 同档。
评级：重点推荐≥78、推荐≥62、可考虑≥48、观察<48。`;

function ScoreGuide() {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative mb-2 flex items-center gap-1 px-1 text-[10px] text-slate-500">
      <span>评分说明</span>
      <button type="button" aria-label="评分说明" onClick={() => setOpen(value => !value)} className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-bold" title={SCORE_GUIDE}>?</button>
      {open && (
        <div role="tooltip" className="absolute left-0 top-5 z-10 w-72 space-y-1.5 rounded-lg border border-slate-200 bg-white p-2 leading-4 shadow-lg">
          <p>
            <span className="font-bold text-slate-700">评分 v2</span>
            <br />
            八维加权，缺失/无效/无法匹配该维记 0 分，不做估算。
          </p>
          <p>
            <span className="font-bold text-slate-700">维度权重</span>
            <br />
            行业兴趣 10%（受众兴趣与行业匹配占比）｜目标地区 8%（受众地区与目标地区匹配占比）｜目标年龄 8%（受众年龄桶与目标年龄相交占比）｜互动表现 20%（近 30 天平均单帖互动量，分档）｜活跃粉丝 15%（有效粉丝率或活跃粉丝/粉丝数）｜内容质量 15%（供应商综合评分）｜粉丝规模 10%（粉丝数，分档）｜互动粉丝比 14%（同一平均互动量/粉丝数，分档）。
          </p>
          <p>
            <span className="font-bold text-slate-700">分档规则</span>
            <br />
            粉丝数 &lt;1万/1-10万/10-50万/50-100万/≥100万 对应 20/40/60/80/100 分；平均互动 &lt;1千/1千-5千/5千-2万/2万-10万/≥10万 同档；互动粉丝比 &lt;0.5%/0.5-1%/1-3%/3-6%/≥6% 同档。
          </p>
          <p>
            <span className="font-bold text-slate-700">评级标准</span>
            <br />
            重点推荐≥78、推荐≥62、可考虑≥48、观察&lt;48。
          </p>
        </div>
      )}
    </div>
  );
}

// 互动率/报价是归一化后的合并字段（_MERGEABLE_FIELDS），落在 fields_json 顶层；防御性取数。
function selectionMetric(item: KolSelectionItem, key: string): number | null {
  return finiteNumber(item.fields?.[key]);
}

interface KolSelectionCardProps {
  item: KolSelectionItem;
  favoriteActive: boolean;
  favoriteBusy: boolean;
  onToggleFavorite: () => void;
  onOpenDetail: () => void;
}

function KolSelectionCard({ item, favoriteActive, favoriteBusy, onToggleFavorite, onOpenDetail }: KolSelectionCardProps) {
  const nickname = item.nickname || '未知达人';
  const stars = stringValue(item.score?.stars);
  const rating = stringValue(item.score?.rating);
  const total = finiteNumber(item.score?.total);
  const version = stringValue(item.score?.version);
  const completeness = finiteNumber(item.score?.data_completeness);
  const engagementRate = selectionMetric(item, 'engagement_rate');
  const quotedPrice = selectionMetric(item, 'quoted_price_cny');
  const metaParts = [
    kolPlatformNames[item.platform] ?? item.platform,
    item.city,
    item.followers != null ? `粉丝 ${formatExposure(item.followers)}` : null,
  ].filter(Boolean);

  return (
    <section className="rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm">
      <div className="flex items-center gap-2.5">
        <button type="button" aria-label={`查看${nickname}详情`} onClick={onOpenDetail} className="flex min-w-0 flex-1 items-center gap-2.5 text-left">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[13px] font-bold text-indigo-600">
            {nickname.slice(0, 1)}
          </span>
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5">
              <span className="truncate text-[12px] font-semibold text-slate-800">{nickname}</span>
              {stars && <span className="shrink-0 text-[10px] text-amber-500">{stars}</span>}
            </p>
            <p className="mt-0.5 truncate text-[10px] text-slate-400">{metaParts.join(' · ')}</p>
          </div>
          {(total != null || rating) && (
            <div className="shrink-0 text-right">
              {total != null && (
                <p className="text-[12px] font-bold text-slate-800">
                  <span className="mr-1 text-[10px] font-normal text-slate-400">综合评分</span>{total}
                </p>
              )}
              {rating && (
                <span className="mt-1 inline-block rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-600">{rating}</span>
              )}
            </div>
          )}
        </button>
        <FavoriteStar active={favoriteActive} busy={favoriteBusy} onToggle={onToggleFavorite} />
      </div>
      {(engagementRate != null || quotedPrice != null) && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {engagementRate != null && (
            <span className="rounded-lg bg-emerald-50 px-2 py-1 text-[10px] font-semibold text-emerald-600">互动率 {engagementRate}%</span>
          )}
          {quotedPrice != null && (
            <span className="rounded-lg bg-slate-100 px-2 py-1 text-[10px] font-semibold text-slate-600">预估报价 ¥{formatNumber(quotedPrice)}</span>
          )}
        </div>
      )}
      {version === 'kol_score_v2' && completeness != null && (
        <p className="mt-2 text-[10px] text-slate-400">数据完整度 {completeness}%</p>
      )}
    </section>
  );
}

function formatScopeText(scope: Record<string, unknown> | null | undefined): string {
  if (!scope) return '';
  const parts: string[] = [];
  if (typeof scope.brand === 'string' && scope.brand) parts.push(`品牌：${scope.brand}`);
  if (typeof scope.campaign === 'string' && scope.campaign) parts.push(`活动：${scope.campaign}`);
  const period = scope.period;
  if (period && typeof period === 'object') {
    const start = String((period as Record<string, unknown>).start ?? '');
    const end = String((period as Record<string, unknown>).end ?? '');
    if (start && end) parts.push(`${start}~${end}`);
  }
  return parts.join(' · ');
}

function ReportBlocks({ report }: { report: ApiAnalysisReport }) {
  const blocks = (report.blocks ?? [])
    .filter(block => block && typeof block === 'object')
    .filter(blockHasContent);
  return (
    <div className="space-y-3">
      {blocks.map((block, index) => (
        <Fragment key={`${block.type}-${index}`}>{ReportBlockView({ block })}</Fragment>
      ))}
      {textOf(report.conclusion) && (
        <Card title="AI 结论" icon={<Sparkles className="h-4 w-4" />}>
          <p className="whitespace-pre-wrap text-[11px] leading-5 text-slate-600">{report.conclusion}</p>
        </Card>
      )}
      {blocks.length === 0 && !textOf(report.conclusion) && (
        <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-400">报告内容为空</p>
      )}
    </div>
  );
}

const versionSelectClass = 'shrink-0 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600 shadow-sm';

/** 品牌/活动一级 Tab：该类型最新报告 + 版本下拉 + scope + 失败提示。 */
function TypedReportPanel({ sessionId, reportType, summaryEntry, emptyText }: {
  sessionId?: string;
  reportType: SessionReportType;
  summaryEntry?: ApiArtifactModuleSummary;
  emptyText: string;
}) {
  const [versions, setVersions] = useState<ApiSessionReportItem[]>([]);
  const [selectedReportId, setSelectedReportId] = useState<string>();
  const [report, setReport] = useState<ApiAnalysisReport>();
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const loadingMessage = useLoadingMessage(loading || detailLoading);

  // 版本列表：会话/类型切换时重拉并选中最新一版。
  useEffect(() => {
    setVersions([]);
    setSelectedReportId(undefined);
    setReport(undefined);
    setDetailLoading(false);
    if (!sessionId) return;
    let cancelled = false;
    setLoading(true);
    listSessionReports(sessionId, reportType)
      .then(items => {
        if (cancelled) return;
        setVersions(items);
        setSelectedReportId(items[0]?.report_id);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, reportType]);

  // 选中版本变化时拉详情；成功失败都复位 detailLoading，不永久卡加载态。
  useEffect(() => {
    if (!selectedReportId) {
      setReport(undefined);
      setDetailLoading(false);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    getAnalysisReport(selectedReportId)
      .then(detail => {
        if (cancelled) return;
        setReport(detail);
        setDetailLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setReport(undefined);
        setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedReportId]);

  const scopeText = formatScopeText(
    report?.scope ?? versions.find(item => item.report_id === selectedReportId)?.scope,
  );
  const failedArtifact = summaryEntry?.latest_artifact?.status === 'failed'
    ? summaryEntry.latest_artifact
    : undefined;

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50/40 p-3">
      <div className="mb-3 flex items-start justify-between gap-2 px-1">
        <div className="min-w-0">
          <h3 className="truncate text-[12px] font-bold text-slate-800">{report?.title || emptyText}</h3>
          {report && (
            <p className="mt-0.5 text-[9px] text-slate-400">
              {scopeText && <span>{scopeText} · </span>}
              报告 v{report.version} · {new Date(report.generated_at).toLocaleString('zh-CN')}
            </p>
          )}
        </div>
        {versions.length > 0 && (
          <select
            aria-label="报告版本"
            value={selectedReportId ?? ''}
            onChange={event => setSelectedReportId(event.target.value)}
            className={versionSelectClass}
          >
            {versions.map(item => (
              <option key={item.report_id} value={item.report_id}>v{item.version}</option>
            ))}
          </select>
        )}
      </div>
      {failedArtifact && (
        <p role="alert" className="mb-3 rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-600">
          上一次报告生成失败，可在会话中重新发起分析
        </p>
      )}
      {loading || detailLoading ? (
        <p role="status" className="flex items-center justify-center gap-2 p-6 text-center text-xs text-slate-400">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {loadingMessage}
        </p>
      ) : report ? (
        <ReportBlocks report={report} />
      ) : (
        <div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">
          {emptyText}
        </div>
      )}
    </div>
  );
}

/** 「达人」一级 Tab：KOL 分析 + 圈选达人两子 Tab（原 UniversalReport 主体）。 */
function KolPanel({ report, taskStatus, sessionId, selectionCount, onReportReady, favorites = [], onFavoriteToggled }: UniversalReportProps) {
  const [analyzing, setAnalyzing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [actionError, setActionError] = useState<string>();
  const [activeTab, setActiveTab] = useState<'report' | 'selection'>('report');
  const [selectionItems, setSelectionItems] = useState<KolSelectionItem[]>([]);
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [selectionError, setSelectionError] = useState(false);
  const [selectionRefresh, setSelectionRefresh] = useState(0);
  const [favoriteBusyKey, setFavoriteBusyKey] = useState<string | null>(null);
  const [reportVersions, setReportVersions] = useState<ApiSessionReportItem[]>([]);
  const [viewReport, setViewReport] = useState<ApiAnalysisReport>();
  const [selectionSets, setSelectionSets] = useState<ApiSelectionSetItem[]>([]);
  const [selectedSetId, setSelectedSetId] = useState<string>();
  const [trendItems, setTrendItems] = useState<KolTop10TrendItem[]>([]);
  const [detailItem, setDetailItem] = useState<KolSelectionItem>();

  // 面板实例跨会话复用：切换会话时重置本地操作状态，避免把上一个会话的 loading/错误带过来。
  useEffect(() => {
    setAnalyzing(false);
    setExporting(false);
    setActionError(undefined);
    setActiveTab('report');
    setSelectionItems([]);
    setSelectionLoading(false);
    setSelectionError(false);
    setReportVersions([]);
    setViewReport(undefined);
    setSelectionSets([]);
    setSelectedSetId(undefined);
    setTrendItems([]);
    setDetailItem(undefined);
  }, [sessionId]);

  // kol_analysis 报告版本列表：会话切换/分析成功后重拉（选中版本在 props.report 更新时复位）。
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    listSessionReports(sessionId, 'kol_analysis')
      .then(items => {
        if (!cancelled) setReportVersions(items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [sessionId, selectionRefresh]);

  useEffect(() => {
    setViewReport(undefined);
  }, [report?.id]);

  // 圈选达人 tab 激活时拉取名单（切 tab / 换会话 / 换名单版本 / 手动刷新计数）。
  useEffect(() => {
    if (activeTab !== 'selection' || !sessionId) return;
    let cancelled = false;
    setSelectionLoading(true);
    setSelectionError(false);
    const request = selectedSetId
      ? getKolSelection(sessionId, selectedSetId)
      : getKolSelection(sessionId);
    request
      .then((data) => {
        if (cancelled) return;
        setSelectionItems(Array.isArray(data.items) ? data.items : []);
        setSelectionLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setSelectionError(true);
        setSelectionLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, sessionId, selectionRefresh, selectedSetId]);

  // 名单版本列表：报告与圈选子页共用同一版本。
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    listSelectionSets(sessionId)
      .then(items => {
        if (!cancelled) setSelectionSets(items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [sessionId, selectionRefresh]);

  useEffect(() => {
    if (!sessionId || activeTab !== 'report') return;
    let cancelled = false;
    getKolTop10Trend(sessionId, selectedSetId).then(data => {
      if (!cancelled) setTrendItems(Array.isArray(data.items) ? data.items : []);
    }).catch(() => { if (!cancelled) setTrendItems([]); });
    return () => { cancelled = true; };
  }, [activeTab, sessionId, selectedSetId, selectionRefresh]);

  // 任务状态仅在「变为终态」的跃迁时刷新名单（正在达人 tab 才刷），避免中间态每次变化重复拉取。
  const taskSettled = isTerminal(taskStatus);
  const prevSettledRef = useRef(taskSettled);
  useEffect(() => {
    const wasSettled = prevSettledRef.current;
    prevSettledRef.current = taskSettled;
    if (taskSettled && !wasSettled) {
      setSelectionRefresh(tick => tick + 1);
    }
  }, [taskSettled, activeTab]);

  const handleAnalyze = async () => {
    if (!sessionId || analyzing) return;
    setAnalyzing(true);
    setActionError(undefined);
    try {
      const nextReport = await runKolAnalysis(sessionId);
      onReportReady?.(nextReport);
      // 分析成功说明圈选名单与报告版本都有更新，正在达人 tab 时同步刷新列表。
      setSelectionRefresh(tick => tick + 1);
    } catch (reason) {
      setActionError(analyzeErrorMessage(reason));
    } finally {
      setAnalyzing(false);
    }
  };

  const handleExport = async () => {
    if (!sessionId || exporting) return;
    setExporting(true);
    setActionError(undefined);
    try {
      if (selectedSetId) {
        await downloadKolSelection(sessionId, selectedSetId);
      } else {
        await downloadKolSelection(sessionId);
      }
    } catch (reason) {
      setActionError(reason instanceof Error && reason.message === 'NO_KOL_SELECTION'
        ? '暂无圈选达人，请先在会话中完成圈选'
        : '导出失败，请稍后重试');
    } finally {
      setExporting(false);
    }
  };

  const isKolFavorited = (item: KolSelectionItem) =>
    favorites.some(favorite => favorite.platform === item.platform && favorite.kol_uid === item.kol_uid);

  // 快照防御取数：缺字段直接省略该键。
  const favoriteSnapshot = (item: KolSelectionItem): Record<string, unknown> => {
    const snapshot: Record<string, unknown> = {};
    if (item.followers != null) snapshot.followers = item.followers;
    const total = finiteNumber(item.score?.total);
    if (total != null) snapshot.score_total = total;
    const rating = stringValue(item.score?.rating);
    if (rating) snapshot.rating = rating;
    const stars = stringValue(item.score?.stars);
    if (stars) snapshot.stars = stars;
    const engagementRate = selectionMetric(item, 'engagement_rate');
    if (engagementRate != null) snapshot.engagement_rate = engagementRate;
    const quotedPrice = selectionMetric(item, 'quoted_price_cny');
    if (quotedPrice != null) snapshot.quoted_price_cny = quotedPrice;
    if (item.city) snapshot.city = item.city;
    if (item.profile_url) snapshot.profile_url = item.profile_url;
    return snapshot;
  };

  const toggleKolFavorite = async (item: KolSelectionItem) => {
    const key = `${item.platform}-${item.kol_uid}`;
    if (favoriteBusyKey === key) return;
    setFavoriteBusyKey(key);
    try {
      if (isKolFavorited(item)) {
        await deleteFavoriteByKey(item.platform, item.kol_uid);
      } else {
        await createFavoriteByKey({
          platform: item.platform,
          kolUid: item.kol_uid,
          nickname: item.nickname || undefined,
          snapshot: favoriteSnapshot(item),
        });
      }
      onFavoriteToggled?.();
    } catch (reason) {
      console.warn('favorite toggle failed', reason);
    } finally {
      setFavoriteBusyKey(current => (current === key ? null : current));
    }
  };

  const displayedReport = viewReport ?? report;
  const selectedCount = selectionCount ?? 0;
  // 名单按综合评分（score.total）倒序展示 Top 20：无评分（null/非数值）排最后，保持原有相对顺序。
  const topSelectionItems = selectionItems
    .map((item, index) => ({ item, index, total: finiteNumber(item.score?.total) }))
    .sort((a, b) => {
      if (a.total == null && b.total == null) return a.index - b.index;
      if (a.total == null) return 1;
      if (b.total == null) return -1;
      return b.total - a.total || a.index - b.index;
    })
    .slice(0, 20)
    .map(entry => entry.item);
  const emptyText = taskStatus === 'insufficient_balance'
    ? '积分不足，任务已停止'
    : selectedCount > 0
      ? `已圈选 ${selectedCount} 位达人，点击「分析」生成 KOL 分析报告`
      : '尚未圈选达人，请先在会话中发起圈选';

  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-slate-200 bg-white px-4">
        <div className="min-w-0">
          <h2 className="truncate text-xs font-bold uppercase tracking-widest text-slate-800">{displayedReport?.title || '智能分析报告'}</h2>
          {displayedReport && (
            <p className="mt-0.5 flex items-center gap-1.5 text-[9px] text-slate-400">
              {reportVersions.length > 0 ? (
                <select
                  aria-label="报告版本"
                  value={viewReport?.id ?? report?.id ?? ''}
                  onChange={(event) => {
                    const reportId = event.target.value;
                    if (!reportId || reportId === report?.id) {
                      setViewReport(undefined);
                      return;
                    }
                    void getAnalysisReport(reportId).then(setViewReport).catch(() => undefined);
                  }}
                  className={versionSelectClass}
                >
                  {reportVersions.map(item => (
                    <option key={item.report_id} value={item.report_id}>v{item.version}</option>
                  ))}
                </select>
              ) : (
                <span>报告 v{displayedReport.version} · </span>
              )}
              {new Date(displayedReport.generated_at).toLocaleString('zh-CN')}
            </p>
          )}
        </div>
        {sessionId && (
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => void handleAnalyze()}
              disabled={analyzing}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {analyzing ? '分析中…' : '分析'}
            </button>
            <button
              type="button"
              onClick={() => void handleExport()}
              disabled={exporting}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-semibold text-slate-600 shadow-sm transition hover:bg-slate-50 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {exporting ? '导出中…' : '导出 Excel'}
            </button>
          </div>
        )}
      </header>
      <div role="tablist" aria-label="报告面板" className="flex h-11 shrink-0 border-b border-slate-200 bg-white px-4">
        {([
          { id: 'report' as const, label: 'KOL 分析' },
          { id: 'selection' as const, label: `圈选达人 (${selectedCount})` },
        ]).map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={activeTab === id}
            onClick={() => setActiveTab(id)}
            className={activeTab === id
              ? 'flex shrink-0 items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
              : 'flex shrink-0 items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto bg-slate-50/40 p-3">
        {activeTab === 'selection' ? (
          <>
            {selectionSets.length > 0 && (
              <div className="mb-2.5 flex justify-end">
                <select
                  aria-label="名单版本"
                  value={selectedSetId ?? ''}
                  onChange={event => setSelectedSetId(event.target.value || undefined)}
                  className={versionSelectClass}
                >
                  <option value="">最新名单</option>
                  {selectionSets.map(item => (
                    <option key={item.set_id} value={item.set_id}>
                      {item.title} v{item.version}（{item.item_count}人）
                    </option>
                  ))}
                </select>
              </div>
            )}
            {selectionLoading ? (
              <p role="status" className="p-6 text-center text-xs text-slate-400">加载中…</p>
            ) : selectionError ? (
              <p role="alert" className="rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-600">达人名单加载失败，请稍后重试</p>
            ) : selectionItems.length === 0 ? (
              <div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">
                暂无圈选达人，发起会话后自动圈选
              </div>
            ) : (
              <div className="space-y-2.5">
                <ScoreGuide />
                {selectionItems.length > 20 && (
                  <p className="px-1 text-[10px] text-slate-400">
                    共 {selectionItems.length} 位达人，按综合评分展示 Top 20
                  </p>
                )}
                {topSelectionItems.map(item => (
                  <Fragment key={`${item.platform}-${item.kol_uid}`}>
                    <KolSelectionCard
                    item={item}
                    favoriteActive={isKolFavorited(item)}
                    favoriteBusy={favoriteBusyKey === `${item.platform}-${item.kol_uid}`}
                    onToggleFavorite={() => void toggleKolFavorite(item)}
                    onOpenDetail={() => setDetailItem(item)}
                    />
                  </Fragment>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            {actionError && (
              <p role="alert" className="mb-3 rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-600">{actionError}</p>
            )}
            {displayedReport ? (
              <>
                <Top10KolTrendChart items={trendItems} />
                {taskStatus && !isTerminal(taskStatus) && !viewReport && (
                  <p role="status" className="mb-3 rounded-lg bg-indigo-50 px-2.5 py-2 text-[11px] text-indigo-600">任务进行中，报告内容可能继续更新…</p>
                )}
                <ReportBlocks report={displayedReport} />
              </>
            ) : (
              <>{trendItems.length > 0 && <div className="mb-3"><Top10KolTrendChart items={trendItems} /></div>}<div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">{emptyText}</div></>
            )}
          </>
        )}
      </div>
      {sessionId && detailItem && (
        <KolSelectionDetailDialog
          sessionId={sessionId}
          setId={selectedSetId}
          item={detailItem}
          onClose={() => setDetailItem(undefined)}
        />
      )}
    </>
  );
}

type TopTabId = 'brand' | 'campaign' | 'kol';

const TOP_TABS: { id: TopTabId; label: string }[] = [
  { id: 'brand', label: '品牌分析' },
  { id: 'campaign', label: '活动分析' },
  { id: 'kol', label: '达人' },
];

export default function UniversalReport(props: UniversalReportProps) {
  const { artifactsSummary, onMarkArtifactSeen, sessionId } = props;
  const [topTab, setTopTab] = useState<TopTabId>('kol');

  // 切换会话回到默认一级 Tab；任务完成/artifact.updated 不自动切换（保持当前 Tab）。
  useEffect(() => {
    setTopTab('kol');
  }, [sessionId]);

  const unreadOf = (id: TopTabId): boolean => {
    if (!artifactsSummary) return false;
    if (id === 'kol') {
      return Boolean(artifactsSummary.kol_analysis?.unread || artifactsSummary.kol_selection?.unread);
    }
    return Boolean(artifactsSummary[id]?.unread);
  };

  const handleSelect = (id: TopTabId) => {
    setTopTab(id);
    const modules: ArtifactModuleKey[] = id === 'kol' ? ['kol_analysis', 'kol_selection'] : [id];
    for (const moduleKey of modules) {
      const entry = artifactsSummary?.[moduleKey];
      if (entry?.unread && entry.latest_artifact) {
        onMarkArtifactSeen?.(moduleKey, entry.latest_artifact.artifact_id);
      }
    }
  };

  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]">
      <div role="tablist" aria-label="分析报告" className="flex h-11 shrink-0 border-b border-slate-200 bg-white px-4">
        {TOP_TABS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={topTab === id}
            onClick={() => handleSelect(id)}
            className={topTab === id
              ? 'flex shrink-0 items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
              : 'flex shrink-0 items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
          >
            {label}
            {unreadOf(id) && (
              <span aria-label="未读" className="h-1.5 w-1.5 rounded-full bg-rose-500" />
            )}
          </button>
        ))}
      </div>
      {topTab === 'kol' ? (
        <KolPanel {...props} />
      ) : (
        <TypedReportPanel
          sessionId={sessionId}
          reportType={topTab === 'brand' ? 'brand_analysis' : 'campaign_analysis'}
          summaryEntry={artifactsSummary?.[topTab]}
          emptyText={topTab === 'brand' ? '完成一次品牌分析后在此展示' : '完成一次活动分析后在此展示'}
        />
      )}
    </aside>
  );
}

export { UniversalReport };
