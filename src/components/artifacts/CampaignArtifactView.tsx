import { Activity, FileText, Heart, LayoutDashboard, Lightbulb, Megaphone, Sparkles, Users } from 'lucide-react';
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type { CampaignReportPayload } from '../../api/agentArtifacts';
import { Card, Missing, restrictedCount, restrictedRatio, restrictedScore } from '../reportPrimitives';

const PLATFORM_NAMES: Record<string, string> = {
  xiaohongshu: '小红书', douyin: '抖音', bilibili: 'B站', kuaishou: '快手', weibo: '微博',
};

function platformName(platform: string): string {
  return PLATFORM_NAMES[platform] ?? platform;
}

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

function TimelineChart({ timeline }: { timeline: CampaignReportPayload['data']['timeline'] }) {
  const dates = [...new Set(timeline.map(item => item.date))].sort();
  const rows = dates.map(date => {
    const row: Record<string, string | number> = { date };
    let volume = 0;
    let engagement = 0;
    for (const item of timeline) {
      if (item.date !== date) continue;
      volume += item.volume ?? 0;
      engagement += item.engagement ?? 0;
    }
    row.声量 = volume;
    row.互动 = engagement;
    return row;
  });
  return (
    <div className="h-44" aria-label="活动声量趋势图表">
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

function TopPosts({ posts }: { posts: CampaignReportPayload['data']['top_posts'] }) {
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
                {url ? <a href={url} target="_blank" rel="noreferrer" className="hover:text-indigo-600 hover:underline">{title}</a> : title}
              </p>
              <p className="mt-1 text-[10px] text-slate-400">
                {platformName(post.platform)} · {post.author || '未知达人'}{post.published_at ? ` · ${post.published_at}` : ''}
              </p>
            </div>
            <p className="shrink-0 text-[10px] text-slate-400">互动 {restrictedScore(post.engagement)}</p>
          </article>
        );
      })}
    </div>
  );
}

export default function CampaignArtifactView({ payload }: { payload: CampaignReportPayload }) {
  const { data, narrative, scope, data_status, limitations } = payload;

  return (
    <div className="space-y-3">
      {data_status === 'restricted' && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="text-[11px] font-semibold text-amber-700">数据受限</p>
          {limitations.map(limitation => (
            <p key={limitation.code} className="mt-1 text-[10px] leading-4 text-amber-600">{limitation.message}</p>
          ))}
        </div>
      )}

      <section data-chapter="overview">
        <Card title="概览" icon={<LayoutDashboard className="h-4 w-4" />}>
          <p className="mb-3 text-[10px] text-slate-400">
            <span className="font-semibold text-slate-600">{scope.brand}</span>
            {scope.campaign ? <span> · {scope.campaign}</span> : null}
          </p>
          <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
            <Metric label="总声量" value={restrictedCount(data.overview.total_volume)} />
            <Metric label="总互动" value={restrictedScore(data.overview.total_engagement)} />
            <Metric label="内容量" value={restrictedScore(data.overview.total_posts)} />
            <Metric label="达人量" value={restrictedScore(data.overview.total_creators)} />
            <Metric label="情感指数" value={restrictedRatio(data.overview.sentiment_score)} />
          </div>
        </Card>
      </section>

      <section data-chapter="platform_contributions">
        <Card title="平台贡献" icon={<Megaphone className="h-4 w-4" />}>
          <DataTable
            rows={data.platform_contributions.map(item => ({
              平台: platformName(item.platform),
              声量: restrictedCount(item.volume),
              互动: restrictedScore(item.engagement),
              内容量: restrictedScore(item.posts),
              达人: restrictedScore(item.creators),
              占比: restrictedRatio(item.share),
            }))}
            columns={[
              { key: '平台', label: '平台' },
              { key: '声量', label: '声量' },
              { key: '互动', label: '互动' },
              { key: '内容量', label: '内容量' },
              { key: '达人', label: '达人' },
              { key: '占比', label: '占比' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="timeline">
        <Card title="时间线" icon={<Activity className="h-4 w-4" />}>
          {data.timeline.length > 0 ? <TimelineChart timeline={data.timeline} /> : <Missing label="数据不足" />}
        </Card>
      </section>

      <section data-chapter="kol_contributions">
        <Card title="KOL 贡献" icon={<Users className="h-4 w-4" />}>
          <DataTable
            rows={data.kol_contributions.map(item => ({
              平台: platformName(item.platform),
              达人: item.nickname || item.kol_uid,
              内容量: restrictedScore(item.posts),
              声量: restrictedCount(item.volume),
              互动: restrictedScore(item.engagement),
              贡献占比: restrictedRatio(item.contribution_share),
            }))}
            columns={[
              { key: '平台', label: '平台' },
              { key: '达人', label: '达人' },
              { key: '内容量', label: '内容量' },
              { key: '声量', label: '声量' },
              { key: '互动', label: '互动' },
              { key: '贡献占比', label: '贡献占比' },
            ]}
          />
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

      <section data-chapter="sentiment">
        <Card title="情感分析" icon={<Heart className="h-4 w-4" />}>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              {([
                ['正面', data.sentiment.summary.positive],
                ['中性', data.sentiment.summary.neutral],
                ['负面', data.sentiment.summary.negative],
              ] as const).map(([label, item]) => (
                <p key={label} className="flex items-center justify-between text-[11px] text-slate-500">
                  <span>{label}</span>
                  <b className="text-slate-700">{restrictedScore(item.count)} 篇 · {restrictedRatio(item.share)}</b>
                </p>
              ))}
            </div>
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
          </div>
        </Card>
      </section>

      <section data-chapter="top_posts">
        <Card title="热帖" icon={<Sparkles className="h-4 w-4" />}>
          <TopPosts posts={data.top_posts} />
        </Card>
      </section>

      <section data-chapter="narrative">
        <Card title="执行摘要" icon={<Sparkles className="h-4 w-4" />}>
          <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{narrative.executive_summary}</p>
        </Card>
        {narrative.phase_review.length > 0 && (
          <Card title="阶段复盘" icon={<Activity className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.phase_review.map((phase, index) => (
                <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-slate-700">{phase.phase}</p>
                  <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{phase.detail}</p>
                </li>
              ))}
            </ul>
          </Card>
        )}
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
