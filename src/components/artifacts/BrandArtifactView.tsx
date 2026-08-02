import {
  Activity, ExternalLink, Globe2, Hash, Heart, LayoutDashboard, Lightbulb, PieChart as PieChartIcon,
  Sparkles, Users, FileText,
} from 'lucide-react';
import {
  Bar, BarChart, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';

import type { BrandReportPayload } from '../../api/agentArtifacts';
import { Card, Missing, restrictedCount, restrictedRatio, restrictedScore } from '../reportPrimitives';

const CHART_COLORS = ['#4f46e5', '#14b8a6', '#f59b00', '#ec4899', '#0ea5e9', '#8b5cf6', '#22c55e', '#64748b'];

/** 概览指标卡：null 显示「数据受限」而非 0。 */
function Metric({ label, value }: { label: string; value: string }) {
  const isRestricted = value === '数据受限';
  return (
    <div className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3 shadow-sm">
      <p className="text-[12px] font-medium text-slate-400">{label}</p>
      <p className={`mt-2 truncate text-[22px] font-bold leading-none tracking-tight ${isRestricted ? 'text-slate-300' : 'text-slate-800'}`}>
        {value}
      </p>
    </div>
  );
}

function platformName(platform: string): string {
  const names: Record<string, string> = { xiaohongshu: '小红书', douyin: '抖音', bilibili: 'B站', kuaishou: '快手', weibo: '微博' };
  return names[platform] ?? platform;
}

function periodText(period: { start: string; end: string } | null): string {
  return period ? `${period.start}~${period.end}` : '';
}

/** 基础明细表格，单元格值为 null 时显示「数据受限」。 */
function DataTable({ rows, columns }: {
  rows: Array<Record<string, string>>;
  columns: Array<{ key: string; label: string }>;
}) {
  if (rows.length === 0) return <Missing label="数据不足" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[11px] text-slate-600">
        <thead>
          <tr>
            {columns.map(column => (
              <th key={column.key} className="border-b border-slate-100 px-2 py-1.5 text-left font-semibold text-slate-500">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="odd:bg-slate-50/60">
              {columns.map(column => (
                <td key={column.key} className="px-2 py-1.5">{row[column.key]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TrendChart({ data }: { data: BrandReportPayload['data']['daily_trend'] }) {
  const dates = [...new Set(data.map(item => item.date))].sort();
  const rows = dates.map(date => {
    const row: Record<string, string | number | null> = { date };
    let volume: number | null = 0;
    let engagement: number | null = 0;
    for (const item of data) {
      if (item.date !== date) continue;
      volume += item.volume ?? 0;
      engagement += item.engagement ?? 0;
    }
    row.声量 = volume;
    row.互动 = engagement;
    return row;
  });
  return (
    <div className="h-44" aria-label="品牌声量趋势图表">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
          <XAxis dataKey="date" tickFormatter={value => String(value).slice(5)} tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} />
          <Tooltip formatter={(value) => [restrictedCount(Number(value)), '数值']} />
          <Line type="monotone" dataKey="声量" stroke="#4f46e5" strokeWidth={2} dot={{ r: 3 }} connectNulls />
          <Line type="monotone" dataKey="互动" stroke="#14b8a6" strokeWidth={2} dot={{ r: 3 }} connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function SentimentChart({ summary }: { summary: BrandReportPayload['data']['sentiment']['summary'] }) {
  const data = [
    { name: '正面', value: summary.positive.count },
    { name: '中性', value: summary.neutral.count },
    { name: '负面', value: summary.negative.count },
  ].filter(item => item.value > 0);
  if (data.length === 0) return <Missing label="数据不足" />;
  return (
    <div className="flex items-center gap-2">
      <div className="h-[110px] w-[110px] shrink-0" aria-label="情感占比环形图">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" innerRadius={28} outerRadius={48} paddingAngle={2} stroke="none">
              {data.map((item, index) => <Cell key={item.name} fill={CHART_COLORS[index % CHART_COLORS.length]} />)}
            </Pie>
            <Tooltip formatter={(value) => [restrictedCount(Number(value)), '篇']} />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <ul className="min-w-0 flex-1 space-y-1.5">
        {data.map((item, index) => (
          <li key={item.name} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
            <span className="flex min-w-0 items-center gap-1.5 truncate">
              <i className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />
              {item.name}
            </span>
            <b className="text-slate-700">{restrictedCount(item.value)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TopPostsList({ posts }: { posts: BrandReportPayload['data']['top_posts'] }) {
  if (posts.length === 0) return <Missing label="数据不足" />;
  return (
    <div className="divide-y divide-slate-100">
      {posts.slice(0, 20).map((post, index) => {
        const url = post.url;
        const title = post.title || '无标题';
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
                {platformName(post.platform)} · {post.author || '未知达人'}{post.published_at ? ` · ${post.published_at}` : ''}
              </p>
            </div>
            <div className="shrink-0 text-right text-[10px] text-slate-400">
              <p>赞 {restrictedScore(post.likes)} · 评 {restrictedScore(post.comments)} · 转 {restrictedScore(post.shares)}</p>
              <p className="mt-0.5">互动 {restrictedScore(post.engagement)}</p>
            </div>
            {url && <ExternalLink className="h-3.5 w-3.5 shrink-0 text-slate-300" aria-hidden="true" />}
          </article>
        );
      })}
    </div>
  );
}

export default function BrandArtifactView({ payload }: { payload: BrandReportPayload }) {
  const { data, narrative, scope, data_status, limitations } = payload;
  const overview = data.overview;

  return (
    <div className="space-y-3">
      {data_status === 'restricted' && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-700">数据受限</p>
          {limitations.map(limitation => (
            <p key={limitation.code} className="mt-1 text-[10px] leading-4 text-amber-600">{limitation.message}</p>
          ))}
        </div>
      )}

      <section data-chapter="overview">
        <Card title="概览" icon={<LayoutDashboard className="h-4 w-4" />}>
          <p className="mb-3 text-[10px] text-slate-400">
            <span className="font-semibold text-slate-600">{scope.brand}</span>
            {periodText(scope.period) && <span> · {periodText(scope.period)}</span>}
          </p>
          <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
            <Metric label="总声量" value={restrictedCount(overview.total_volume)} />
            <Metric label="总互动" value={restrictedScore(overview.total_engagement)} />
            <Metric label="内容量" value={restrictedScore(overview.total_posts)} />
            <Metric label="情感指数" value={restrictedRatio(overview.sentiment_score)} />
          </div>
          {overview.platforms.length > 0 && (
            <div className="mt-3">
              <p className="mb-1.5 text-[11px] font-semibold text-slate-600">平台表现</p>
              <DataTable
                rows={overview.platforms.map(item => ({
                  平台: platformName(item.platform),
                  声量: restrictedCount(item.volume),
                  互动: restrictedScore(item.engagement),
                  内容量: restrictedScore(item.posts),
                  声量占比: restrictedRatio(item.share_of_voice),
                  情感: restrictedRatio(item.sentiment_score),
                }))}
                columns={[
                  { key: '平台', label: '平台' },
                  { key: '声量', label: '声量' },
                  { key: '互动', label: '互动' },
                  { key: '内容量', label: '内容量' },
                  { key: '声量占比', label: '声量占比' },
                  { key: '情感', label: '情感' },
                ]}
              />
            </div>
          )}
        </Card>
      </section>

      <section data-chapter="sentiment">
        <Card title="情感分析" icon={<Heart className="h-4 w-4" />}>
          <div className="grid gap-3 md:grid-cols-2">
            <SentimentChart summary={data.sentiment.summary} />
            {data.sentiment.by_platform.length > 0 && (
              <DataTable
                rows={data.sentiment.by_platform.map(item => ({
                  平台: platformName(item.platform),
                  正面: restrictedScore(item.positive.count),
                  中性: restrictedScore(item.neutral.count),
                  负面: restrictedScore(item.negative.count),
                }))}
                columns={[
                  { key: '平台', label: '平台' },
                  { key: '正面', label: '正面' },
                  { key: '中性', label: '中性' },
                  { key: '负面', label: '负面' },
                ]}
              />
            )}
          </div>
        </Card>
      </section>

      <section data-chapter="daily_trend">
        <Card title="声量趋势" icon={<Activity className="h-4 w-4" />}>
          {data.daily_trend.length > 0 ? <TrendChart data={data.daily_trend} /> : <Missing label="数据不足" />}
        </Card>
      </section>

      <section data-chapter="content_types">
        <Card title="内容类型" icon={<FileText className="h-4 w-4" />}>
          <DataTable
            rows={data.content_types.map(item => ({
              平台: platformName(item.platform),
              类型: item.type,
              内容量: restrictedScore(item.posts),
              声量: restrictedCount(item.volume),
              互动: restrictedScore(item.engagement),
            }))}
            columns={[
              { key: '平台', label: '平台' },
              { key: '类型', label: '类型' },
              { key: '内容量', label: '内容量' },
              { key: '声量', label: '声量' },
              { key: '互动', label: '互动' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="creators">
        <Card title="创作者分层" icon={<Users className="h-4 w-4" />}>
          {data.creator_tiers.length > 0 && (
            <div className="mb-3">
              <DataTable
                rows={data.creator_tiers.map(item => ({
                  平台: platformName(item.platform),
                  层级: item.tier,
                  达人: restrictedScore(item.creator_count),
                  内容量: restrictedScore(item.posts),
                  声量: restrictedCount(item.volume),
                  互动: restrictedScore(item.engagement),
                }))}
                columns={[
                  { key: '平台', label: '平台' },
                  { key: '层级', label: '层级' },
                  { key: '达人', label: '达人' },
                  { key: '内容量', label: '内容量' },
                  { key: '声量', label: '声量' },
                  { key: '互动', label: '互动' },
                ]}
              />
            </div>
          )}
          {data.organic_vs_paid.length > 0 && (
            <div>
              <p className="mb-1.5 text-[11px] font-semibold text-slate-600">投放结构</p>
              <DataTable
                rows={data.organic_vs_paid.map(item => ({
                  平台: platformName(item.platform),
                  类型: item.kind === 'organic' ? '自然' : '付费',
                  内容量: restrictedScore(item.posts),
                  声量: restrictedCount(item.volume),
                  互动: restrictedScore(item.engagement),
                }))}
                columns={[
                  { key: '平台', label: '平台' },
                  { key: '类型', label: '类型' },
                  { key: '内容量', label: '内容量' },
                  { key: '声量', label: '声量' },
                  { key: '互动', label: '互动' },
                ]}
              />
            </div>
          )}
          {data.creator_tiers.length === 0 && data.organic_vs_paid.length === 0 && <Missing label="数据不足" />}
        </Card>
      </section>

      <section data-chapter="regions">
        <Card title="地域分布" icon={<Globe2 className="h-4 w-4" />}>
          <DataTable
            rows={data.regions.map(item => ({
              地区: item.region,
              声量: restrictedCount(item.volume),
              占比: restrictedRatio(item.share),
              情感: restrictedRatio(item.sentiment_score),
            }))}
            columns={[
              { key: '地区', label: '地区' },
              { key: '声量', label: '声量' },
              { key: '占比', label: '占比' },
              { key: '情感', label: '情感' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="topics">
        <Card title="话题洞察" icon={<Hash className="h-4 w-4" />}>
          <DataTable
            rows={data.topics.map(item => ({
              话题: item.topic,
              声量: restrictedCount(item.volume),
              互动: restrictedScore(item.engagement),
              情感: restrictedRatio(item.sentiment_score),
            }))}
            columns={[
              { key: '话题', label: '话题' },
              { key: '声量', label: '声量' },
              { key: '互动', label: '互动' },
              { key: '情感', label: '情感' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="top_posts">
        <Card title="热帖" icon={<PieChartIcon className="h-4 w-4" />}>
          <TopPostsList posts={data.top_posts} />
        </Card>
      </section>

      <section data-chapter="narrative">
        <Card title="执行摘要" icon={<Sparkles className="h-4 w-4" />}>
          <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{narrative.executive_summary}</p>
        </Card>
        {narrative.findings.length > 0 && (
          <Card title="发现" icon={<Lightbulb className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.findings.map((finding, index) => (
                <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-slate-700">{finding.title}</p>
                  {finding.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{finding.detail}</p>}
                </li>
              ))}
            </ul>
          </Card>
        )}
        {narrative.recommendations.length > 0 && (
          <Card title="建议" icon={<Lightbulb className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.recommendations.map((recommendation, index) => (
                <li key={index} className="rounded-lg border border-indigo-50 bg-indigo-50/50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-indigo-700">{recommendation.title}</p>
                  {recommendation.action && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{recommendation.action}</p>}
                </li>
              ))}
            </ul>
          </Card>
        )}
      </section>

    </div>
  );
}
