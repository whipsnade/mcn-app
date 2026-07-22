import {
  Activity, BarChart2, Database, PieChart as PieChartIcon, Sparkles, Table as TableIcon, Tags,
} from 'lucide-react';
import {
  Bar, BarChart, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Fragment, useState } from 'react';

import { downloadKolSelection, runKolAnalysis } from '../api/kolSelection';
import type {
  ApiAnalysisReport, ApiAnalysisReportChartSeries, ApiAnalysisReportMetricItem, ApiTaskStatus, ReportBlock,
} from '../api/contracts';
import { Card, formatNumber, MetricCard } from './reportPrimitives';

interface UniversalReportProps {
  report?: ApiAnalysisReport;
  taskStatus?: ApiTaskStatus | string;
  sessionId?: string;
  selectionCount?: number;
  onReportReady?: (report: ApiAnalysisReport) => void;
}

const chartColors = ['#4f46e5', '#818cf8', '#14b8a6', '#f59b00', '#0ea5e9', '#f43f4f'];

function isTerminal(status?: string): boolean {
  return status === 'completed' || status === 'completed_with_warnings' || status === 'insufficient_balance';
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

export default function UniversalReport({ report, taskStatus, sessionId, selectionCount, onReportReady }: UniversalReportProps) {
  const [analyzing, setAnalyzing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [actionError, setActionError] = useState<string>();

  const handleAnalyze = async () => {
    if (!sessionId || analyzing) return;
    setAnalyzing(true);
    setActionError(undefined);
    try {
      const nextReport = await runKolAnalysis(sessionId);
      onReportReady?.(nextReport);
    } catch (reason) {
      setActionError(reason instanceof Error && reason.message === 'NO_KOL_SELECTION'
        ? '暂无圈选达人，请先在会话中完成圈选'
        : '分析失败，请稍后重试');
    } finally {
      setAnalyzing(false);
    }
  };

  const handleExport = async () => {
    if (!sessionId || exporting) return;
    setExporting(true);
    setActionError(undefined);
    try {
      await downloadKolSelection(sessionId);
    } catch (reason) {
      setActionError(reason instanceof Error && reason.message === 'NO_KOL_SELECTION'
        ? '暂无圈选达人，请先在会话中完成圈选'
        : '导出失败，请稍后重试');
    } finally {
      setExporting(false);
    }
  };

  const blocks = (report?.blocks ?? [])
    .filter(block => block && typeof block === 'object')
    .filter(blockHasContent);
  const selectedCount = selectionCount ?? 0;
  const emptyText = taskStatus === 'insufficient_balance'
    ? '积分不足，任务已停止'
    : selectedCount > 0
      ? `已圈选 ${selectedCount} 位达人，点击「分析」生成 KOL 分析报告`
      : '尚未圈选达人，请先在会话中发起圈选';

  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]">
      <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-slate-200 bg-white px-4">
        <div className="min-w-0">
          <h2 className="truncate text-xs font-bold uppercase tracking-widest text-slate-800">{report?.title || '智能分析报告'}</h2>
          {report && (
            <p className="mt-0.5 text-[9px] text-slate-400">报告 v{report.version} · {new Date(report.generated_at).toLocaleString('zh-CN')}</p>
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
      <div className="flex-1 overflow-y-auto bg-slate-50/40 p-3">
        {actionError && (
          <p role="alert" className="mb-3 rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-600">{actionError}</p>
        )}
        {report ? (
          <>
            {taskStatus && !isTerminal(taskStatus) && (
              <p role="status" className="mb-3 rounded-lg bg-indigo-50 px-2.5 py-2 text-[11px] text-indigo-600">任务进行中，报告内容可能继续更新…</p>
            )}
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
          </>
        ) : (
          <div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">
            {emptyText}
          </div>
        )}
        <Card title="品牌/活动分析（即将上线）" icon={<PieChartIcon className="h-4 w-4" />} className="mt-3">
          <p className="text-[11px] text-slate-400">品牌与活动维度分析即将上线，敬请期待。</p>
        </Card>
      </div>
    </aside>
  );
}

export { UniversalReport };
