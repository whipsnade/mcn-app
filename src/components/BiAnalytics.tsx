import {
  Activity,
  HelpCircle,
  MessageCircleHeart,
  PieChart as PieChartIcon,
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
import type { ReactNode } from 'react';

import type {
  ApiTaskStatus,
  BiAnalyticsData,
  BiDistribution,
  BiMetric,
} from '../api/contracts';

interface BiAnalyticsProps {
  analytics?: BiAnalyticsData;
  taskStatus?: ApiTaskStatus | string;
}

const chartIndigo = '#5b5ce2';
const sentimentColors = ['#14b887', '#f59b00', '#f43f4f'];

function isTerminal(status?: string): boolean {
  return status === 'completed' || status === 'completed_with_warnings';
}

function Card({
  title,
  icon,
  hint,
  children,
  className = '',
}: {
  title: string;
  icon: ReactNode;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm ${className}`}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="flex min-w-0 items-center gap-1.5 text-[14px] font-bold text-slate-700">
          <span className="shrink-0 text-indigo-500">{icon}</span>
          <span className="truncate">{title}</span>
          <HelpCircle aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-slate-300" />
        </h3>
        {hint && <span className="shrink-0 text-[10px] text-slate-400">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

function Missing({ label = '数据不足' }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[42px] items-center justify-center rounded-lg bg-slate-50 px-2 text-[11px] text-slate-400">
      {label}
    </div>
  );
}

function metricAvailable(metric: BiMetric | undefined): metric is BiMetric {
  return Boolean(metric?.available && metric.value !== null && Number.isFinite(metric.value));
}

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

function formatExposure(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}M`;
  if (value >= 10_000) return `${(value / 10_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}万`;
  return formatNumber(value);
}

function MetricCard({ label, metric, format = 'number', accent = 'text-slate-800' }: { label: string; metric?: BiMetric; format?: 'number' | 'exposure' | 'percent'; accent?: string }) {
  const value = metricAvailable(metric)
    ? format === 'percent'
      ? `${metric.value}%`
      : format === 'exposure'
        ? formatExposure(metric.value)
        : formatNumber(metric.value)
    : '数据不足';
  return (
    <section className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3.5 shadow-sm">
      <div className="flex items-center gap-1 text-[12px] font-medium text-slate-400">
        <span className="truncate">{label}</span>
        <HelpCircle aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-slate-300" />
      </div>
      <p className={`mt-2 truncate text-[24px] font-bold leading-none tracking-tight ${metricAvailable(metric) ? accent : 'text-slate-300'}`}>
        {value}
      </p>
      {metricAvailable(metric) && <p className="mt-1 text-[10px] text-slate-400">{metric.unit}</p>}
    </section>
  );
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
  const trend = analytics?.exposure_trend ?? [];
  const chartData = trend.map(item => ({ ...item, dateLabel: item.date.slice(5) }));
  return (
    <Card title="活动传播周期与曝光走势" icon={<Activity className="h-4 w-4" />} hint="7天核心数据监测">
      {chartData.length < 2 ? <div className="h-[175px]"><Missing /></div> : (
        <div className="h-[175px]" aria-label="活动传播周期与曝光走势折线图">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
              <XAxis dataKey="dateLabel" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
              <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={{ stroke: '#94a3b8' }} tickFormatter={formatExposure} width={48} />
              <Tooltip formatter={(value) => [formatExposure(Number(value)), '曝光量']} labelFormatter={label => `日期：${label}`} />
              <Line type="monotone" dataKey="value" stroke={chartIndigo} strokeWidth={2} dot={{ r: 3, fill: chartIndigo, strokeWidth: 0 }} activeDot={{ r: 5 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

function AudienceDistribution({ distribution, label }: { distribution?: BiDistribution; label: string }) {
  const items = distribution?.available ? distribution.items.filter(item => Number.isFinite(item.value)) : [];
  return (
    <div className="min-w-0">
      <p className="mb-2 text-[11px] font-semibold text-slate-400">{label}</p>
      {items.length === 0 ? <Missing /> : (
        <div className="space-y-2">
          {items.slice(0, 5).map(item => (
            <div key={item.label} className="flex items-center gap-2 text-[11px]">
              <span className="w-[58px] shrink-0 truncate text-slate-500">{item.label}</span>
              <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-indigo-500" style={{ width: `${Math.min(100, Math.max(0, item.value))}%` }} /></div>
              <b className="w-8 text-right text-slate-600">{item.value}%</b>
            </div>
          ))}
        </div>
      )}
    </div>
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
        <MetricCard label="全网品牌声量" metric={usableAnalytics?.overview.brand_volume} />
        <MetricCard label="总曝光量" metric={usableAnalytics?.overview.total_exposure} format="exposure" />
        <MetricCard label="平均互动率" metric={usableAnalytics?.overview.average_engagement_rate} format="percent" accent="text-slate-800" />
      </div>
      <SentimentCard analytics={usableAnalytics} />
      <TrendCard analytics={usableAnalytics} />
      <AudienceCard analytics={usableAnalytics} />
      {!usableAnalytics && <p className="px-1 text-[10px] text-slate-400">任务完成后将展示本轮可追溯的社媒数据分析。</p>}
    </div>
  );
}

export { AudienceDistribution, BiAnalytics };
