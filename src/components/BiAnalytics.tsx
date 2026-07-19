import {
  Activity,
  MessageCircleHeart,
  Users,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type {
  ApiTaskStatus,
  BiAnalyticsData,
  BiMetric,
} from '../api/contracts';
import { Card, formatExposure, MetricCard, Missing } from './reportPrimitives';

interface BiAnalyticsProps {
  analytics?: BiAnalyticsData;
  taskStatus?: ApiTaskStatus | string;
}

const chartIndigo = '#5b5ce2';
const sentimentColors = ['#14b887', '#f59b00', '#f43f4f'];

function isTerminal(status?: string): boolean {
  return status === 'completed' || status === 'completed_with_warnings';
}

function metricUsable(metric?: BiMetric): boolean {
  return Boolean(metric?.available && metric.value !== null && Number.isFinite(metric.value));
}

function distributionUsable(distribution?: { available?: boolean; items?: unknown[] }): boolean {
  return Boolean(distribution?.available && (distribution.items?.length ?? 0) > 0);
}

/**
 * 品牌与 KOL 双通道受众分析按字段合并：hybrid 任务中品牌证据失败时，
 * 数据分析页回退展示候选达人的受众画像，而不是整页“数据不足”。
 */
export function mergeAnalyticsChannels(
  primary?: BiAnalyticsData,
  fallback?: BiAnalyticsData,
): BiAnalyticsData | undefined {
  // 旧报告可能只有 {} 形状的空通道，按缺失处理。
  if (!primary?.overview || !primary?.audience) return fallback;
  if (!fallback?.overview || !fallback?.audience) return primary;
  return {
    overview: {
      brand_volume: metricUsable(primary.overview.brand_volume)
        ? primary.overview.brand_volume
        : fallback.overview.brand_volume,
      total_exposure: metricUsable(primary.overview.total_exposure)
        ? primary.overview.total_exposure
        : fallback.overview.total_exposure,
      average_engagement_rate: metricUsable(primary.overview.average_engagement_rate)
        ? primary.overview.average_engagement_rate
        : fallback.overview.average_engagement_rate,
    },
    sentiment: primary.sentiment?.available ? primary.sentiment : fallback.sentiment,
    exposure_trend: primary.exposure_trend?.length
      ? primary.exposure_trend
      : fallback.exposure_trend,
    volume_trend: primary.volume_trend?.length ? primary.volume_trend : fallback.volume_trend,
    sentiment_trend: primary.sentiment_trend?.length
      ? primary.sentiment_trend
      : fallback.sentiment_trend,
    audience: {
      age: distributionUsable(primary.audience.age)
        ? primary.audience.age
        : fallback.audience.age,
      gender: distributionUsable(primary.audience.gender)
        ? primary.audience.gender
        : fallback.audience.gender,
      regions: distributionUsable(primary.audience.regions)
        ? primary.audience.regions
        : fallback.audience.regions,
    },
  };
}

function SentimentCard({ analytics }: { analytics?: BiAnalyticsData }) {
  const sentiment = analytics?.sentiment;
  const items = sentiment?.available ? sentiment.items.filter(item => Number.isFinite(item.percentage)) : [];
  const chartData = items.map(item => ({ name: item.label, value: item.percentage }));
  const hotWords = sentiment?.hot_words ?? [];
  return (
    <Card title="舆情情感极性分析" icon={<MessageCircleHeart className="h-4 w-4" />} hint="粉丝真实评论抽样">
      {items.length === 0 ? <Missing /> : (
        <>
          <div className="flex items-center gap-2">
            <div className="h-[130px] w-[130px] shrink-0" aria-label="情感极性环形图">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={chartData} dataKey="value" nameKey="name" innerRadius={34} outerRadius={57} paddingAngle={2} stroke="none">
                    {chartData.map((item, index) => <Cell key={item.name} fill={sentimentColors[index % sentimentColors.length]} />)}
                  </Pie>
                  <Tooltip formatter={(value) => [`${value}%`, '占比']} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="min-w-0 flex-1 space-y-2.5">
              {items.map((item, index) => (
                <div key={item.key}>
                  <div className="mb-1 flex items-center justify-between text-[12px] font-semibold text-slate-600">
                    <span className="flex items-center gap-1.5"><i className="h-2 w-2 rounded-full" style={{ backgroundColor: sentimentColors[index % sentimentColors.length] }} />{item.label}</span>
                    <span>{item.percentage}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full" style={{ width: `${Math.min(100, Math.max(0, item.percentage))}%`, backgroundColor: sentimentColors[index % sentimentColors.length] }} /></div>
                </div>
              ))}
            </div>
          </div>
          <div className="mt-4 border-t border-slate-100 pt-3">
            <p className="mb-2 text-[11px] font-semibold text-slate-400">评论区高热词云：</p>
            <div className="flex flex-wrap gap-2">
              {hotWords.length === 0 ? <Missing label="暂无热词数据" /> : hotWords.slice(0, 8).map((word, index) => (
                <span key={`${word.term}-${index}`} className={`rounded-lg px-2.5 py-1.5 text-[11px] font-semibold ${index % 3 === 0 ? 'bg-emerald-50 text-emerald-600' : index % 3 === 1 ? 'bg-slate-100 text-slate-500' : 'bg-indigo-50 text-indigo-600'}`}>
                  {word.term}
                </span>
              ))}
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function TrendCard({ analytics }: { analytics?: BiAnalyticsData }) {
  const brandTrend = analytics?.volume_trend ?? [];
  const chartData = brandTrend.length > 0
    ? brandTrend.map(item => ({ value: item.value, unit: item.unit, platforms: item.platforms, dateLabel: item.period.slice(5) }))
    : (analytics?.exposure_trend ?? []).map(item => ({ ...item, dateLabel: item.date.slice(5) }));
  return (
    <Card title={brandTrend.length > 0 ? '品牌声量变化趋势' : '活动传播周期与曝光走势'} icon={<Activity className="h-4 w-4" />} hint="跨平台真实数据">
      {chartData.length < 2 ? <div className="h-[175px]"><Missing /></div> : (
        <div className="h-[175px]" aria-label="活动传播周期与曝光走势折线图">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
              <XAxis dataKey="dateLabel" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
              <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} tickFormatter={formatExposure} width={48} />
              <Tooltip formatter={(value) => [formatExposure(Number(value)), brandTrend.length > 0 ? '品牌声量' : '曝光量']} labelFormatter={label => `周期：${label}`} />
              <Line type="monotone" dataKey="value" stroke={chartIndigo} strokeWidth={2} dot={{ r: 3, fill: chartIndigo, strokeWidth: 0 }} activeDot={{ r: 5 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

function SentimentTrendCard({ analytics }: { analytics?: BiAnalyticsData }) {
  const trend = analytics?.sentiment_trend ?? [];
  const chartData = trend.map(item => ({ period: item.period.slice(5), value: item.value }));
  return (
    <Card title="用户情感趋势" icon={<MessageCircleHeart className="h-4 w-4" />} hint="情感指数">
      {chartData.length < 2 ? <div className="h-[145px]"><Missing label="暂无情感趋势数据" /></div> : (
        <div className="h-[145px]" aria-label="用户情感趋势折线图">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
              <XAxis dataKey="period" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
              <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} width={42} />
              <Tooltip formatter={(value) => [Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }), '情感指数']} labelFormatter={label => `周期：${label}`} />
              <Line type="monotone" dataKey="value" stroke="#14b887" strokeWidth={2} dot={{ r: 3, fill: '#14b887', strokeWidth: 0 }} activeDot={{ r: 5 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

function AudienceCard({ analytics }: { analytics?: BiAnalyticsData }) {
  const audience = analytics?.audience;
  const ageItems = audience?.age?.available ? audience.age.items.filter(item => Number.isFinite(item.value)) : [];
  const genderItems = audience?.gender?.available ? audience.gender.items.filter(item => Number.isFinite(item.value)) : [];
  const ageChart = ageItems.map(item => ({ name: item.label, value: item.value }));
  return (
    <Card title="粉丝客群/受众人口统计画像" icon={<Users className="h-4 w-4" />} hint="多维画像重叠">
      <div className="grid grid-cols-2 gap-3">
        <div className="min-w-0">
          <p className="mb-2 text-[11px] font-semibold text-slate-400">年龄段分布（%）</p>
          {ageChart.length === 0 ? <div className="h-[126px]"><Missing /></div> : (
            <div className="h-[126px]" aria-label="年龄段分布柱状图">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ageChart} margin={{ top: 4, right: 4, bottom: 0, left: -20 }}>
                  <XAxis dataKey="name" tick={{ fontSize: 9, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} />
                  <YAxis tick={{ fontSize: 9, fill: '#94a3b8' }} tickLine={false} axisLine={false} width={28} />
                  <Tooltip formatter={(value) => [`${value}%`, '占比']} />
                  <Bar dataKey="value" fill="#818cf8" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
        <div className="min-w-0">
          <p className="mb-2 text-[11px] font-semibold text-slate-400">性别比例</p>
          {genderItems.length === 0 ? <Missing /> : (
            <div className="space-y-2">
              {genderItems.slice(0, 4).map((item, index) => (
                <div key={item.label} className="rounded-lg border border-slate-100 bg-slate-50 px-2.5 py-2">
                  <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600"><span>{item.label}</span><span>{item.value}%</span></div>
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-200"><div className={`h-full rounded-full ${index === 0 ? 'bg-pink-500' : 'bg-blue-500'}`} style={{ width: `${Math.min(100, Math.max(0, item.value))}%` }} /></div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="mt-4 border-t border-slate-100 pt-3">
        <p className="mb-2 text-[11px] font-semibold text-slate-400">受众省份排名前 5（地区）：</p>
        {audience?.regions?.available && audience.regions.items.length > 0 ? (
          <div className="grid grid-cols-5 gap-1.5">
            {audience.regions.items.slice(0, 5).map(item => (
              <div key={item.label} className="rounded-lg border border-slate-100 bg-slate-50 px-1.5 py-2 text-center">
                <p className="truncate text-[10px] text-slate-500">{item.label}</p>
                <p className="mt-0.5 text-[12px] font-bold text-indigo-600">{item.value}%</p>
              </div>
            ))}
          </div>
        ) : <Missing />}
      </div>
    </Card>
  );
}

export default function BiAnalytics({ analytics, taskStatus }: BiAnalyticsProps) {
  const usableAnalytics = isTerminal(taskStatus) || taskStatus === undefined ? analytics : undefined;
  return (
    <div role="region" aria-label="数据分析" className="space-y-3">
      <div className="grid grid-cols-3 gap-2.5">
        <MetricCard label="全网品牌声量" metric={usableAnalytics?.overview?.brand_volume} />
        <MetricCard label="总曝光量" metric={usableAnalytics?.overview?.total_exposure} format="exposure" />
        <MetricCard label="平均互动率" metric={usableAnalytics?.overview?.average_engagement_rate} format="percent" accent="text-slate-800" />
      </div>
      <SentimentCard analytics={usableAnalytics} />
      <TrendCard analytics={usableAnalytics} />
      <SentimentTrendCard analytics={usableAnalytics} />
      <AudienceCard analytics={usableAnalytics} />
      {!usableAnalytics && <p className="px-1 text-[10px] text-slate-400">任务完成后将展示本轮可追溯的社媒数据分析。</p>}
    </div>
  );
}

export { BiAnalytics };
