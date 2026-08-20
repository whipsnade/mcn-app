import { Activity, BarChart2, FileText, Link2, Table2, TrendingUp } from 'lucide-react';
import { Fragment, type ReactNode } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
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
  AnalysisReportBlock,
  AnalysisReportCell,
  AnalysisReportChartBlock,
  AnalysisReportColumnType,
  AnalysisReportPayload,
  AnalysisReportTypedTableBlock,
} from '../../api/agentArtifacts';
import { Card, formatNumber } from '../reportPrimitives';
import { safeHttpUrl } from './urlUtils';

const CHART_COLORS = ['#4f46e5', '#14b8a6', '#f59e0b', '#ec4899', '#0ea5e9', '#8b5cf6', '#22c55e', '#64748b'];

function formatNumberValue(value: number): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function formatCell(value: AnalysisReportCell, type?: AnalysisReportColumnType): ReactNode {
  if (value === null) return <span className="text-slate-400">数据受限</span>;
  if (type === 'url') {
    const url = safeHttpUrl(value);
    return url ? (
      <a href={url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">
        {String(value)}
      </a>
    ) : <span className="text-amber-600">链接不可用</span>;
  }
  if (type === 'percent' && typeof value === 'number') {
    return `${(value * 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`;
  }
  if (typeof value === 'number') return formatNumberValue(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  return value;
}

function blockTitle(block: AnalysisReportBlock): string {
  return block.title || '报告模块';
}

function MetricCardsBlock({ block }: { block: Extract<AnalysisReportBlock, { block_type: 'metric_cards' }> }) {
  return (
    <Card title={blockTitle(block)} icon={<BarChart2 className="h-4 w-4" />}>
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        {block.cards.map(card => (
          <section key={card.key} className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3 shadow-sm">
            <p className="truncate text-[12px] font-medium text-slate-400">{card.label}</p>
            <p className={`mt-2 truncate text-[22px] font-bold leading-none tracking-tight ${card.value === null ? 'text-slate-300' : 'text-slate-800'}`}>
              {formatCell(card.value, card.value_type)}
            </p>
            {card.value !== null && card.unit && <p className="mt-1 text-[10px] text-slate-400">{card.unit}</p>}
          </section>
        ))}
      </div>
    </Card>
  );
}

function TypedTableBlock({ block }: { block: AnalysisReportTypedTableBlock }) {
  return (
    <Card title={blockTitle(block)} icon={<Table2 className="h-4 w-4" />}>
      {block.columns.length === 0 || block.rows.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-4 text-center text-[11px] text-slate-400">数据不足</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[11px] text-slate-600">
            <thead>
              <tr>
                {block.columns.map(column => (
                  <th key={column.key} className="whitespace-nowrap border-b border-slate-100 px-2 py-1.5 text-left font-semibold text-slate-500">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="odd:bg-slate-50/60">
                  {block.columns.map((column, columnIndex) => (
                    <td key={column.key} className="whitespace-nowrap px-2 py-1.5">
                      {formatCell(row[columnIndex] ?? null, column.type)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function TimeSeriesBlock({ block }: { block: Extract<AnalysisReportBlock, { block_type: 'time_series' }> }) {
  const keys = [...new Set(block.points.flatMap(point => Object.keys(point.values)))];
  return (
    <Card title={blockTitle(block)} icon={<TrendingUp className="h-4 w-4" />}>
      {block.points.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-4 text-center text-[11px] text-slate-400">数据不足</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[11px] text-slate-600">
            <thead>
              <tr>
                <th className="whitespace-nowrap border-b border-slate-100 px-2 py-1.5 text-left font-semibold text-slate-500">时间</th>
                {keys.map(key => <th key={key} className="whitespace-nowrap border-b border-slate-100 px-2 py-1.5 text-left font-semibold text-slate-500">{key}</th>)}
              </tr>
            </thead>
            <tbody>
              {block.points.map(point => (
                <tr key={point.timestamp} className="odd:bg-slate-50/60">
                  <td className="whitespace-nowrap px-2 py-1.5">{point.timestamp}</td>
                  {keys.map(key => <td key={key} className="whitespace-nowrap px-2 py-1.5">{formatCell(point.values[key] ?? null, 'number')}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function LinkListBlock({ block }: { block: Extract<AnalysisReportBlock, { block_type: 'link_list' }> }) {
  return (
    <Card title={blockTitle(block)} icon={<Link2 className="h-4 w-4" />}>
      {block.items.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-4 text-center text-[11px] text-slate-400">数据不足</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {block.items.map((item, index) => {
            const url = safeHttpUrl(item.url);
            return (
              <li key={`${item.label}-${index}`} className="py-2.5 text-[11px]">
                {url ? (
                  <a href={url} target="_blank" rel="noreferrer" className="font-semibold text-indigo-600 hover:underline">{item.label}</a>
                ) : (
                  <span className="font-semibold text-slate-700">{item.label}</span>
                )}
                {!url && <span className="ml-2 text-amber-600">链接不可用</span>}
                {item.description && <p className="mt-1 text-slate-400">{item.description}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

type ChartDatum = { category: string; [key: string]: string | number | null };

function chartData(block: AnalysisReportChartBlock): ChartDatum[] {
  return block.categories.map((category, index) => {
    const datum: ChartDatum = { category };
    for (const series of block.series) datum[series.key] = series.values[index] ?? null;
    return datum;
  });
}

function ChartBlock({ block }: { block: AnalysisReportChartBlock }) {
  const data = chartData(block);
  if (data.length === 0 || block.series.length === 0) {
    return <Card title={blockTitle(block)} icon={<Activity className="h-4 w-4" />}><p className="text-[11px] text-slate-400">数据不足</p></Card>;
  }
  if (block.chart_type === 'pie') {
    const series = block.series[0];
    const pieData = data.flatMap(item => {
      const value = item[series.key];
      return typeof value === 'number' && Number.isFinite(value) ? [{ name: item.category, value }] : [];
    });
    return (
      <Card title={blockTitle(block)} icon={<BarChart2 className="h-4 w-4" />}>
        {pieData.length === 0 ? <p className="text-[11px] text-slate-400">数据受限</p> : (
          <div className="h-48" aria-label={`${blockTitle(block)}图表`}>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={32} outerRadius={60} paddingAngle={2} stroke="none">
                  {pieData.map((item, index) => <Cell key={item.name} fill={CHART_COLORS[index % CHART_COLORS.length]} />)}
                </Pie>
                <Tooltip formatter={(value) => [value == null ? '数据受限' : formatNumber(Number(value)), '数值']} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    );
  }
  const Chart = block.chart_type === 'bar' ? BarChart : block.chart_type === 'area' ? AreaChart : LineChart;
  return (
    <Card title={blockTitle(block)} icon={<Activity className="h-4 w-4" />}>
      <div className="h-48" aria-label={`${blockTitle(block)}图表`}>
        <ResponsiveContainer width="100%" height="100%">
          <Chart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="category" tick={{ fontSize: 10, fill: '#94a3b8' }} />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} />
            <Tooltip formatter={(value) => [value == null ? '数据受限' : formatNumber(Number(value)), '数值']} />
            {block.series.map((series, index) => block.chart_type === 'bar' ? (
              <Bar key={series.key} dataKey={series.key} name={series.label} fill={CHART_COLORS[index % CHART_COLORS.length]} />
            ) : block.chart_type === 'area' ? (
              <Area key={series.key} type="monotone" dataKey={series.key} name={series.label} stroke={CHART_COLORS[index % CHART_COLORS.length]} fill={CHART_COLORS[index % CHART_COLORS.length]} fillOpacity={0.18} />
            ) : (
              <Line key={series.key} type="monotone" dataKey={series.key} name={series.label} stroke={CHART_COLORS[index % CHART_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} />
            ))}
          </Chart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

function NarrativeBlock({ block }: { block: Extract<AnalysisReportBlock, { block_type: 'narrative' }> }) {
  return (
    <Card title={blockTitle(block)} icon={<FileText className="h-4 w-4" />}>
      <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{block.content}</p>
    </Card>
  );
}

function MethodologyBlock({ block }: { block: Extract<AnalysisReportBlock, { block_type: 'methodology_limitations' }> }) {
  return (
    <Card title={blockTitle(block)} icon={<FileText className="h-4 w-4" />}>
      <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{block.methodology}</p>
      {block.limitations.length > 0 && (
        <ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] text-amber-700">
          {block.limitations.map(item => <li key={item}>{item}</li>)}
        </ul>
      )}
    </Card>
  );
}

function renderBlock(block: AnalysisReportBlock): ReactNode {
  switch (block.block_type) {
    case 'metric_cards': return <Fragment key={block.id}><MetricCardsBlock block={block} /></Fragment>;
    case 'typed_table': return <Fragment key={block.id}><TypedTableBlock block={block} /></Fragment>;
    case 'time_series': return <Fragment key={block.id}><TimeSeriesBlock block={block} /></Fragment>;
    case 'link_list': return <Fragment key={block.id}><LinkListBlock block={block} /></Fragment>;
    case 'chart': return <Fragment key={block.id}><ChartBlock block={block} /></Fragment>;
    case 'narrative': return <Fragment key={block.id}><NarrativeBlock block={block} /></Fragment>;
    case 'methodology_limitations': return <Fragment key={block.id}><MethodologyBlock block={block} /></Fragment>;
    default: {
      const future = block as unknown as { id?: string; title?: string };
      return (
        <Fragment key={future.id ?? 'unknown'}>
          <Card title={future.title ?? '未来报告模块'} icon={<FileText className="h-4 w-4" />}>
            <p className="text-[11px] text-slate-500">暂不支持的报告模块</p>
          </Card>
        </Fragment>
      );
    }
  }
}

export default function AnalysisReportView({ payload }: { payload: AnalysisReportPayload }) {
  const restricted = payload.data_status === 'restricted'
    || Object.values(payload.availability).some(section => section.status !== 'complete');
  const brand = typeof payload.scope.brand === 'string' ? payload.scope.brand : null;
  const platforms = Array.isArray(payload.scope.platforms)
    ? payload.scope.platforms.filter((platform): platform is string => typeof platform === 'string')
    : [];

  return (
    <div className="space-y-3">
      <header className="rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm">
        <h2 className="text-base font-bold text-slate-800">{payload.title}</h2>
        {(brand || platforms.length > 0) && (
          <p className="mt-1 text-[11px] text-slate-400">
            {brand}
            {brand && platforms.length > 0 ? ' · ' : ''}
            {platforms.join('、')}
          </p>
        )}
        {restricted && (
          <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700">
            <p className="font-semibold">数据受限</p>
            {payload.limitations.map(limitation => <p key={limitation.code} className="mt-1">{limitation.message}</p>)}
          </div>
        )}
      </header>

      {payload.blocks.map(block => renderBlock(block))}

      {payload.fulfillment.length > 0 && (
        <Card title="结果完整性" icon={<Table2 className="h-4 w-4" />}>
          <ul className="space-y-1.5 text-[11px] text-slate-600">
            {payload.fulfillment.map(item => (
              <li key={item.key} className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-semibold">{item.key}</span>
                <span>{item.actual_count}/{item.requested_min} · {item.reason}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
