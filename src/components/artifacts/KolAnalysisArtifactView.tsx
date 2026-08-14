import { Activity, BarChart3, LayoutDashboard, Sparkles, Trophy } from 'lucide-react';

import type {
  AgentArtifactDistributionItem,
  KolAnalysisPayload,
} from '../../api/agentArtifacts';
import { Card, formatNumber, restrictedCount, restrictedRatio, restrictedScore } from '../reportPrimitives';

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

function DistributionList({ title, items }: { title: string; items: AgentArtifactDistributionItem[] }) {
  if (items.length === 0) return null;
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/60 p-3">
      <p className="mb-1.5 text-[11px] font-semibold text-slate-600">{title}</p>
      <ul className="space-y-1">
        {items.map(item => (
          <li key={item.key} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
            <span className="min-w-0 truncate">{item.label}</span>
            <b className="shrink-0 text-slate-700">{restrictedCount(item.count)} · {restrictedRatio(item.share)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DataTable({ rows, columns }: {
  rows: Array<Record<string, string>>;
  columns: Array<{ key: string; label: string }>;
}) {
  if (rows.length === 0) return <p className="py-4 text-center text-[11px] text-slate-400">暂无数据</p>;
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

export default function KolAnalysisArtifactView({ payload }: { payload: KolAnalysisPayload }) {
  const { data, narrative, data_status, limitations } = payload;

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

      <section data-chapter="summary">
        <Card title="概览" icon={<LayoutDashboard className="h-4 w-4" />}>
          <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
            <Metric label="达人数" value={restrictedScore(data.summary.kol_count)} />
            <Metric label="总粉丝" value={restrictedCount(data.summary.total_followers)} />
            <Metric label="有效粉丝" value={restrictedCount(data.summary.total_active_followers)} />
            <Metric label="总互动" value={restrictedScore(data.summary.total_engagement)} />
            <Metric label="平均评分" value={restrictedScore(data.summary.avg_score)} />
          </div>
        </Card>
      </section>

      <section data-chapter="distributions">
        <Card title="分布" icon={<BarChart3 className="h-4 w-4" />}>
          <div className="grid gap-3 md:grid-cols-2">
            <DistributionList title="平台分布" items={data.platform_distribution} />
            <DistributionList title="评级分布" items={data.rating_distribution} />
            <DistributionList title="粉丝分布" items={data.follower_distribution} />
            <DistributionList title="互动分布" items={data.engagement_distribution} />
            <DistributionList title="地域分布" items={data.region_distribution} />
          </div>
        </Card>
      </section>

      <section data-chapter="kol_trend">
        <Card title="KOL 趋势" icon={<Activity className="h-4 w-4" />}>
          <DataTable
            rows={data.kol_trend.map(item => ({
              平台: platformName(item.platform),
              达人: item.nickname || item.kol_uid,
              粉丝: restrictedCount(item.followers),
              有效粉丝: restrictedCount(item.active_followers),
              总互动: restrictedScore(item.engagement_total),
              均互动: restrictedScore(item.avg_engagement),
              增长: restrictedRatio(item.growth_rate),
              评分: restrictedScore(item.score),
            }))}
            columns={[
              { key: '平台', label: '平台' },
              { key: '达人', label: '达人' },
              { key: '粉丝', label: '粉丝' },
              { key: '有效粉丝', label: '有效粉丝' },
              { key: '总互动', label: '总互动' },
              { key: '均互动', label: '均互动' },
              { key: '增长', label: '增长' },
              { key: '评分', label: '评分' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="top_kols">
        <Card title="Top KOL" icon={<Trophy className="h-4 w-4" />}>
          <DataTable
            rows={data.top_kols.map(item => ({
              rank: String(item.rank),
              平台: platformName(item.platform),
              达人: item.nickname || item.kol_uid,
              评分: restrictedScore(item.score),
              总互动: restrictedScore(item.engagement_total),
              评级: item.rating,
            }))}
            columns={[
              { key: 'rank', label: '排名' },
              { key: '平台', label: '平台' },
              { key: '达人', label: '达人' },
              { key: '评分', label: '评分' },
              { key: '总互动', label: '总互动' },
              { key: '评级', label: '评级' },
            ]}
          />
        </Card>
      </section>

      <section data-chapter="narrative">
        {narrative.executive_summary && (
          <Card title="执行摘要" icon={<Sparkles className="h-4 w-4" />}>
            <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{narrative.executive_summary}</p>
          </Card>
        )}
        {narrative.portfolio_findings.length > 0 && (
          <Card title="组合发现" icon={<Sparkles className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.portfolio_findings.map((finding, index) => (
                <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-slate-700">{finding.title}</p>
                  {finding.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{finding.detail}</p>}
                </li>
              ))}
            </ul>
          </Card>
        )}
        {narrative.mix_recommendations.length > 0 && (
          <Card title="组合建议" icon={<Sparkles className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.mix_recommendations.map((finding, index) => (
                <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-slate-700">{finding.title}</p>
                  {finding.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{finding.detail}</p>}
                </li>
              ))}
            </ul>
          </Card>
        )}
        {narrative.risk_notes.length > 0 && (
          <Card title="风险提示" icon={<Sparkles className="h-4 w-4" />}>
            <ul className="space-y-2">
              {narrative.risk_notes.map((finding, index) => (
                <li key={index} className="rounded-lg bg-slate-50 px-2.5 py-2">
                  <p className="text-[12px] font-semibold text-slate-700">{finding.title}</p>
                  {finding.detail && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{finding.detail}</p>}
                </li>
              ))}
            </ul>
          </Card>
        )}
      </section>
    </div>
  );
}
