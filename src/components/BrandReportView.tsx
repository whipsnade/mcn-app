import {
  Activity, BarChart2, BookOpen, Flame, MapPin, MessageSquare, PieChart as PieChartIcon, Sparkles, Users,
} from 'lucide-react';
import { Fragment, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type {
  ApiAnalysisReport, BrandReportChapterAvailability, BrandReportMetricComparison,
  BrandReportNarrative, BrandReportPayload, BrandReportPeriodValue, BrandReportTopPost,
} from '../api/contracts';
import { Card, formatNumber, Missing } from './reportPrimitives';

const PLATFORM_NAMES: Record<string, string> = {
  xiaohongshu: '小红书',
  douyin: '抖音',
  bilibili: 'B站',
  kuaishou: '快手',
  weibo: '微博',
};

function platformName(platform: string): string {
  return PLATFORM_NAMES[platform] ?? platform;
}

// 后端 availability/PeriodValue 的 reason 码（brand_assembler.py）→ 中文说明。
const REASON_LABELS: Record<string, string> = {
  invalid_period: '统计周期不合法',
  insufficient_points: '积分不足，部分数据未采集',
  no_data: '查询无数据',
  tool_failed: '数据工具调用失败',
  no_evidence: '未采集到相关证据',
  not_requested: '该周期未取数',
};

const CHAPTERS = [
  { key: 'overview', label: '概览', icon: BarChart2 },
  { key: 'sentiment', label: '情感', icon: PieChartIcon },
  { key: 'daily_trend', label: '趋势', icon: Activity },
  { key: 'content_creators', label: '内容与达人', icon: Users },
  { key: 'regions', label: '地域', icon: MapPin },
  { key: 'top_posts', label: '热帖', icon: Flame },
  { key: 'insights', label: '舆情', icon: MessageSquare },
  { key: 'methodology', label: '方法论', icon: BookOpen },
] as const;

type ChapterKey = (typeof CHAPTERS)[number]['key'];

function fmt(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatNumber(value) : '未提供';
}

function fmtPct(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}%` : '未提供';
}

// 对比期一行：未取数/受限只展示状态，百分比由后端组装器算好，前端不换算。
function periodLine(label: string, period: BrandReportPeriodValue | undefined, pct: number | null | undefined): { text: string; tone: 'up' | 'down' | 'muted' } | null {
  if (!period || period.status === 'not_requested') return { text: `${label} 未取数`, tone: 'muted' };
  if (period.status === 'restricted') return { text: `${label} 受限`, tone: 'muted' };
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return null;
  return { text: `${label} ${pct > 0 ? '+' : ''}${pct}%`, tone: pct < 0 ? 'down' : 'up' };
}

function ComparisonCard({ label, metric }: { label: string; metric?: BrandReportMetricComparison }) {
  const current = metric?.current;
  const hasValue = typeof current === 'number' && Number.isFinite(current);
  const lines = [
    periodLine('环比', metric?.mom, metric?.mom_change_pct),
    periodLine('同比', metric?.yoy, metric?.yoy_change_pct),
  ].filter((line): line is NonNullable<typeof line> => line !== null);
  return (
    <section className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3 shadow-sm">
      <p className="text-[12px] font-medium text-slate-400">{label}</p>
      <p className={`mt-1.5 truncate text-[20px] font-bold leading-none tracking-tight ${hasValue ? 'text-slate-800' : 'text-slate-300'}`}>
        {hasValue ? formatNumber(current) : '数据不足'}
      </p>
      <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5">
        {lines.map(line => (
          <span key={line.text} className={`text-[10px] ${line.tone === 'down' ? 'text-rose-500' : line.tone === 'up' ? 'text-emerald-600' : 'text-slate-400'}`}>
            {line.text}
          </span>
        ))}
      </div>
    </section>
  );
}

function DataTable({ columns, rows }: { columns: string[]; rows: ReactNode[][] }) {
  return (
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
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="px-2 py-1.5 align-top">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OverviewChapter({ payload }: { payload: BrandReportPayload }) {
  const { overview } = payload.data;
  const { scope } = payload;
  const split = overview.sentiment_split;
  return (
    <div className="space-y-2.5">
      <p className="text-[10px] text-slate-400">
        品牌 {scope.brand || '未提供'}
        {scope.period_start && scope.period_end ? ` · ${scope.period_start}~${scope.period_end}` : ''}
        {scope.platforms.length > 0 ? ` · ${scope.platforms.map(platformName).join('、')}` : ''}
        {scope.data_as_of ? ` · 数据截至 ${scope.data_as_of}` : ''}
      </p>
      <div className="grid grid-cols-2 gap-2">
        <ComparisonCard label="总声量" metric={overview.total_mentions} />
        <ComparisonCard label="总互动" metric={overview.total_interactions} />
      </div>
      {overview.platforms.length > 0 && (
        <DataTable
          columns={['平台', '声量', '互动']}
          rows={overview.platforms.map(item => [
            platformName(item.platform),
            fmt(item.mentions),
            fmt(item.interactions),
          ])}
        />
      )}
      {(split.positive != null || split.neutral != null || split.negative != null) && (
        <div className="flex flex-wrap gap-1.5 text-[10px] font-semibold">
          <span className="rounded-lg bg-emerald-50 px-2 py-1 text-emerald-600">正面 {fmt(split.positive)}</span>
          <span className="rounded-lg bg-slate-100 px-2 py-1 text-slate-500">中性 {fmt(split.neutral)}</span>
          <span className="rounded-lg bg-rose-50 px-2 py-1 text-rose-500">负面 {fmt(split.negative)}</span>
        </div>
      )}
    </div>
  );
}

function SentimentChapter({ payload }: { payload: BrandReportPayload }) {
  const rows = payload.data.sentiment.rows;
  if (rows.length === 0) return <Missing label="数据不足" />;
  return (
    <DataTable
      columns={['平台', '情感', '声量', '互动', '占比']}
      rows={rows.map(row => [platformName(row.platform), row.sentiment, fmt(row.mentions), fmt(row.interactions), fmtPct(row.share_pct)])}
    />
  );
}

function TrendChapter({ payload, availability }: { payload: BrandReportPayload; availability?: BrandReportChapterAvailability }) {
  // 受限章节只显示受限说明：禁止用概览的变化字段伪造折线。
  if (availability?.status === 'unavailable') return null;
  const trend = payload.data.daily_trend;
  if (trend.points.length === 0) return <Missing label="数据不足" />;
  const data = trend.points.map(point => ({
    date: point.date.length >= 5 ? point.date.slice(5) : point.date,
    声量: point.mentions,
    互动量: point.interactions,
  }));
  return (
    <div className="space-y-2">
      {trend.peak_date && (
        <p className="text-[10px] text-slate-400">峰值 {trend.peak_date} · 声量 {fmt(trend.peak_mentions)}</p>
      )}
      <div className="h-44" aria-label="日趋势图表">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} width={42} />
            <Tooltip formatter={(value, name) => [formatNumber(Number(value)), name]} />
            <Legend wrapperStyle={{ fontSize: 10 }} />
            <Line type="monotone" dataKey="声量" stroke="#4f46e5" strokeWidth={2} dot={{ r: 2 }} connectNulls />
            <Line type="monotone" dataKey="互动量" stroke="#14b8a6" strokeWidth={2} dot={{ r: 2 }} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function ContentCreatorsChapter({ payload }: { payload: BrandReportPayload }) {
  const { content_types, creator_tiers, organic_vs_paid: ovp } = payload.data;
  const hasOrganic = ovp.organic_mentions != null || ovp.paid_mentions != null;
  if (content_types.length === 0 && creator_tiers.length === 0 && !hasOrganic) return <Missing label="数据不足" />;
  return (
    <div className="space-y-3">
      {hasOrganic && (
        <div className="flex flex-wrap gap-1.5 text-[10px] font-semibold">
          <span className="rounded-lg bg-emerald-50 px-2 py-1 text-emerald-600">
            自然内容 {fmtPct(ovp.organic_share_pct)}（{fmt(ovp.organic_mentions)}）
          </span>
          <span className="rounded-lg bg-indigo-50 px-2 py-1 text-indigo-600">
            商单内容 {fmtPct(ovp.paid_share_pct)}（{fmt(ovp.paid_mentions)}）
          </span>
        </div>
      )}
      {content_types.length > 0 && (
        <div>
          <p className="mb-1 text-[11px] font-semibold text-slate-500">内容类型</p>
          <DataTable
            columns={['内容类型', '声量', '占比']}
            rows={content_types.map(row => [row.content_type, fmt(row.mentions), fmtPct(row.share_pct)])}
          />
        </div>
      )}
      {creator_tiers.length > 0 && (
        <div>
          <p className="mb-1 text-[11px] font-semibold text-slate-500">达人层级</p>
          <DataTable
            columns={['层级', '声量', '占比']}
            rows={creator_tiers.map(row => [row.tier, fmt(row.mentions), fmtPct(row.share_pct)])}
          />
        </div>
      )}
    </div>
  );
}

function RegionsChapter({ payload }: { payload: BrandReportPayload }) {
  const rows = payload.data.regions;
  if (rows.length === 0) return <Missing label="数据不足" />;
  return (
    <DataTable
      columns={['地区', '声量', '互动', '占比']}
      rows={rows.map(row => [row.region, fmt(row.mentions), fmt(row.interactions), fmtPct(row.share_pct)])}
    />
  );
}

function TopPostCard({ post }: { post: BrandReportTopPost }) {
  const [expanded, setExpanded] = useState(false);
  const exposureLabel = post.platform === 'douyin' ? '播放数' : '阅读数';
  const shareLabel = post.platform === 'douyin' ? '分享' : '转发';
  const stats: [string, number | null][] = [
    ['互动量', post.interactions],
    [exposureLabel, post.exposure_count],
    ['点赞', post.like_count],
    ['评论', post.comment_count],
    ['收藏', post.collect_count],
    [shareLabel, post.share_count],
  ];
  return (
    <article className="rounded-lg border border-slate-100 bg-slate-50/60 p-2.5">
      <p className={`text-[11px] font-semibold leading-4 text-slate-700 ${expanded ? '' : 'line-clamp-2'}`}>
        {post.title ?? '未提供'}
      </p>
      <p className="mt-1 text-[10px] text-slate-400">
        作者 {post.author ?? '未提供'} · {post.creator_tier ?? '未提供'} · 情感 {post.sentiment ?? '未提供'}
      </p>
      <dl className="mt-1.5 grid grid-cols-3 gap-1">
        {stats.map(([label, value]) => (
          <div key={label}>
            <dt className="text-[9px] text-slate-400">{label}</dt>
            <dd className="text-[10px] font-semibold text-slate-600">{fmt(value)}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-1.5 flex items-center gap-2">
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}
          className="text-[10px] font-semibold text-indigo-500 hover:text-indigo-600"
        >
          {expanded ? '收起' : '展开'}
        </button>
        {expanded && post.url && (
          <a
            href={post.url}
            target="_blank"
            rel="noreferrer"
            className="text-[10px] font-semibold text-indigo-500 hover:text-indigo-600"
          >
            查看原帖
          </a>
        )}
      </div>
    </article>
  );
}

function TopPostsChapter({ payload }: { payload: BrandReportPayload }) {
  const posts = payload.data.top_posts;
  const [active, setActive] = useState<string | null>(null);
  if (posts.length === 0) return <Missing label="数据不足" />;
  // 平台顺序与后端导出一致：小红书优先，其次抖音，其余按出现顺序。
  const platforms: string[] = [];
  for (const preferred of ['xiaohongshu', 'douyin']) {
    if (posts.some(post => post.platform === preferred)) platforms.push(preferred);
  }
  for (const post of posts) {
    if (!platforms.includes(post.platform)) platforms.push(post.platform);
  }
  const current = active && platforms.includes(active) ? active : platforms[0];
  const list = posts.filter(post => post.platform === current);
  return (
    <div className="space-y-2">
      {platforms.length > 1 && (
        <div className="flex gap-1" aria-label="热帖平台">
          {platforms.map(platform => (
            <button
              key={platform}
              type="button"
              aria-pressed={current === platform}
              onClick={() => setActive(platform)}
              className={current === platform
                ? 'rounded-lg bg-indigo-600 px-2.5 py-1 text-[10px] font-semibold text-white'
                : 'rounded-lg bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-500 hover:bg-slate-200'}
            >
              {platformName(platform)}
            </button>
          ))}
        </div>
      )}
      {list.map((post, index) => (
        <Fragment key={`${post.platform}-${post.post_id ?? index}`}>
          <TopPostCard post={post} />
        </Fragment>
      ))}
    </div>
  );
}

function NarrativeList({ label, items, className }: { label: string; items: string[]; className: string }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className={`mb-1 text-[11px] font-semibold ${className}`}>{label}</p>
      <ul className="list-disc space-y-0.5 pl-4 text-[11px] leading-5 text-slate-600">
        {items.map((item, index) => <li key={index}>{item}</li>)}
      </ul>
    </div>
  );
}

function InsightsChapter({ narrative }: { narrative?: BrandReportNarrative | null }) {
  if (!narrative) return <Missing label="数据不足" />;
  const hasContent = narrative.praise_points.length > 0
    || narrative.complaint_points.length > 0
    || narrative.expansion_signals.length > 0
    || narrative.key_findings.length > 0
    || Boolean(narrative.noise_notes);
  if (!hasContent) return <Missing label="数据不足" />;
  return (
    <div className="space-y-2.5">
      <p className="text-[11px] text-slate-500">
        负面影响程度
        <span className={`ml-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold ${narrative.impact_level === '高' ? 'bg-rose-50 text-rose-600' : narrative.impact_level === '中' ? 'bg-amber-50 text-amber-600' : 'bg-emerald-50 text-emerald-600'}`}>
          {narrative.impact_level}
        </span>
      </p>
      <NarrativeList label="好评点" items={narrative.praise_points} className="text-emerald-600" />
      <NarrativeList label="槽点" items={narrative.complaint_points} className="text-rose-500" />
      <NarrativeList label="扩张信号" items={narrative.expansion_signals} className="text-indigo-500" />
      <NarrativeList label="关键发现" items={narrative.key_findings} className="text-slate-500" />
      {narrative.noise_notes && (
        <p className="rounded-lg bg-slate-50 px-2.5 py-1.5 text-[10px] leading-4 text-slate-400">噪音说明：{narrative.noise_notes}</p>
      )}
    </div>
  );
}

function MethodologyChapter({ payload }: { payload: BrandReportPayload }) {
  const [open, setOpen] = useState(false);
  const { query_spec: querySpec, scope, sources } = payload;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-semibold text-slate-500 hover:bg-slate-50"
      >
        {open ? '收起方法论' : '展开方法论'}
      </button>
      {open && (
        <div className="mt-2 space-y-1.5 text-[11px] leading-5 text-slate-500">
          <p>
            查询口径：{querySpec.original_term || '未提供'}
            {querySpec.matched_tag ? `（匹配标签：${querySpec.matched_tag}）` : ''}
            {!querySpec.matched_tag && querySpec.fallback_keyword ? `（回退关键词：${querySpec.fallback_keyword}）` : ''}
          </p>
          <p>比较期定义：{querySpec.comparison_definition || '未提供'}</p>
          <p>
            对比模式：{scope.comparison_mode === 'mom_yoy' ? '环比 + 同比' : '仅环比'}
            {scope.data_as_of ? ` · 数据截至 ${scope.data_as_of}` : ''}
          </p>
          {sources.length > 0 && (
            <div>
              <p className="font-semibold text-slate-500">数据来源</p>
              <ul className="list-disc pl-4 text-[10px] text-slate-400">
                {sources.map((source, index) => (
                  <li key={`${source.tool}-${index}`}>
                    {source.tool}{source.collected_at ? ` · 采集时间 ${source.collected_at}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChapterCard({ title, icon, availability, children }: {
  title: string;
  icon: ReactNode;
  availability?: BrandReportChapterAvailability;
  children: ReactNode;
}) {
  const restricted = Boolean(availability && availability.status !== 'complete');
  return (
    <Card
      title={title}
      icon={icon}
      badge={restricted ? (
        <span className="shrink-0 rounded-full bg-amber-50 px-1.5 py-0.5 text-[9px] font-semibold text-amber-600">受限</span>
      ) : undefined}
    >
      {restricted && availability && (
        <p className="mb-2 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[10px] text-amber-600">
          数据受限{availability.reason ? `：${REASON_LABELS[availability.reason] ?? availability.reason}` : ''}
          {availability.missing_fields.length > 0 ? `（缺失字段：${availability.missing_fields.join('、')}）` : ''}
        </p>
      )}
      {children}
    </Card>
  );
}

/**
 * brand_report_v2 payload 运行时形状守卫：只查几个关键键（data.overview 对象、
 * data.top_posts 数组、scope 对象、availability 对象的值含 missing_fields 数组），
 * 不做全量校验。落库快照被截断/漂移时返回 false，调用方降级到旧 Block 渲染而非白屏。
 */
export function isBrandReportPayload(value: unknown): value is BrandReportPayload {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<BrandReportPayload>;
  const data = candidate.data;
  if (!data || typeof data !== 'object') return false;
  if (!data.overview || typeof data.overview !== 'object') return false;
  if (!Array.isArray(data.top_posts)) return false;
  if (!candidate.scope || typeof candidate.scope !== 'object') return false;
  const availability = candidate.availability;
  if (!availability || typeof availability !== 'object' || Array.isArray(availability)) return false;
  return Object.values(availability).every(
    entry => Boolean(entry) && typeof entry === 'object'
      && Array.isArray((entry as BrandReportChapterAvailability).missing_fields),
  );
}

export default function BrandReportView({ report }: { report: ApiAnalysisReport }) {
  const [activeChapter, setActiveChapter] = useState<ChapterKey>('overview');
  const sectionRefs = useRef<Partial<Record<ChapterKey, HTMLElement | null>>>({});
  // 防御性：非 brand_report_v2 模板或形状不合格的 payload 走空态，不白屏。
  const payload = report.template_version === 'brand_report_v2' && isBrandReportPayload(report.payload)
    ? report.payload
    : null;
  if (!payload) {
    return <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-400">报告内容为空</p>;
  }

  const availabilityOf = (key: ChapterKey): BrandReportChapterAvailability | undefined => payload.availability?.[key];
  const scrollTo = (key: ChapterKey) => {
    setActiveChapter(key);
    sectionRefs.current[key]?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  };

  const chapterContent: Record<ChapterKey, ReactNode> = {
    overview: <OverviewChapter payload={payload} />,
    sentiment: <SentimentChapter payload={payload} />,
    daily_trend: <TrendChapter payload={payload} availability={availabilityOf('daily_trend')} />,
    content_creators: <ContentCreatorsChapter payload={payload} />,
    regions: <RegionsChapter payload={payload} />,
    top_posts: <TopPostsChapter payload={payload} />,
    insights: <InsightsChapter narrative={payload.narrative} />,
    methodology: <MethodologyChapter payload={payload} />,
  };

  return (
    <div className="space-y-3">
      <nav aria-label="报告章节" className="sticky top-0 z-10 -mx-1 flex gap-1 overflow-x-auto bg-slate-50/95 px-1 py-1">
        {CHAPTERS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            aria-current={activeChapter === key}
            onClick={() => scrollTo(key)}
            className={activeChapter === key
              ? 'shrink-0 rounded-lg bg-indigo-600 px-2.5 py-1 text-[10px] font-semibold text-white'
              : 'shrink-0 rounded-lg bg-white px-2.5 py-1 text-[10px] font-semibold text-slate-500 shadow-sm hover:text-slate-800'}
          >
            {label}
          </button>
        ))}
      </nav>
      {CHAPTERS.map(({ key, label, icon: Icon }) => (
        <section
          key={key}
          id={`brand-chapter-${key}`}
          data-chapter={key}
          // 导航 sticky top-0：锚点滚动到 block:'start' 时留出导航高度，避免章节标题被遮挡。
          className="scroll-mt-10"
          ref={element => {
            sectionRefs.current[key] = element;
          }}
        >
          <ChapterCard title={label} icon={<Icon className="h-4 w-4" />} availability={availabilityOf(key)}>
            {chapterContent[key]}
          </ChapterCard>
        </section>
      ))}
      {payload.narrative && (
        <>
          <Card title="AI 结论" icon={<Sparkles className="h-4 w-4" />}>
            <p className="whitespace-pre-wrap text-[11px] leading-5 text-slate-600">{payload.narrative.conclusion}</p>
          </Card>
          {payload.narrative.recommendations.length > 0 && (
            <Card title="结论与建议" icon={<Sparkles className="h-4 w-4" />}>
              <ul className="list-disc space-y-1 pl-4 text-[11px] leading-5 text-slate-600">
                {payload.narrative.recommendations.map((item, index) => <li key={index}>{item}</li>)}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

export { BrandReportView };
