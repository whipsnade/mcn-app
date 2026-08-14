import type { ReactNode } from 'react';
import { Fragment } from 'react';
import { Activity, BarChart2, FileText, Link2, List, PieChart as PieChartIcon, Table as TableIcon } from 'lucide-react';
import { Bar, BarChart, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type { InsightBoardPayload } from '../../api/agentArtifacts';
import { Card, formatNumber, restrictedScore } from '../reportPrimitives';
import { safeHttpUrl } from './urlUtils';

/** §12.2：insight_board_v1 仅允许以下 8 种 Block；其它类型一律不渲染。 */
const ALLOWED_BLOCK_TYPES = new Set([
  'metric_grid', 'table', 'bar_chart', 'line_chart', 'pie_chart', 'markdown', 'timeline', 'references',
]);

type InsightBlock = InsightBoardPayload['data'][number];

const CHART_COLORS = ['#4f46e5', '#14b8a6', '#f59b00', '#ec4899', '#0ea5e9', '#8b5cf6', '#22c55e', '#64748b'];

function asString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function blockCard(block: InsightBlock, children: ReactNode, icon: ReactNode): ReactNode {
  return <Card title={asString(block.title) || '钻取块'} icon={icon}>{children}</Card>;
}

function MetricGridBlock({ block }: { block: InsightBlock }) {
  const cards = Array.isArray(block.cards)
    ? (block.cards as Array<Record<string, unknown>>).filter(card => card && typeof card === 'object' && asString(card.label))
    : [];
  if (cards.length === 0) return null;
  return blockCard(block, (
    <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
      {cards.map((card, index) => (
        <div key={`${asString(card.label)}-${index}`} className="min-w-0 rounded-xl border border-slate-100 bg-white px-3 py-3 shadow-sm">
          <p className="text-[12px] font-medium text-slate-400">{asString(card.label)}</p>
          <p className="mt-2 truncate text-[22px] font-bold leading-none tracking-tight text-slate-800">
            {restrictedScore(asNumber(card.value))}
          </p>
        </div>
      ))}
    </div>
  ), <BarChart2 className="h-4 w-4" />);
}

function TableBlock({ block }: { block: InsightBlock }) {
  const columns = Array.isArray(block.columns) ? (block.columns as unknown[]).map(asString).filter(Boolean) : [];
  const rows = Array.isArray(block.rows) ? block.rows as unknown[][] : [];
  const validRows = rows.filter(row => Array.isArray(row));
  if (columns.length === 0 || validRows.length === 0) return null;
  return blockCard(block, (
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
          {validRows.map((row, rowIndex) => (
            <tr key={rowIndex} className="odd:bg-slate-50/60">
              {columns.map((_, columnIndex) => (
                <td key={columnIndex} className="px-2 py-1.5">{asString(row[columnIndex])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ), <TableIcon className="h-4 w-4" />);
}

function chartData(block: InsightBlock): Array<Record<string, unknown>> {
  const categories = Array.isArray(block.categories) ? (block.categories as unknown[]).map(asString).filter(Boolean) : [];
  const series = Array.isArray(block.series) ? block.series as Array<Record<string, unknown>> : [];
  return categories.map((name, index) => {
    const row: Record<string, unknown> = { name };
    for (const item of series) {
      const values = Array.isArray(item.values) ? item.values as unknown[] : [];
      row[asString(item.name) || 'value'] = asNumber(values[index]);
    }
    return row;
  });
}

function BarChartBlock({ block }: { block: InsightBlock }) {
  const data = chartData(block);
  const series = Array.isArray(block.series) ? block.series as Array<Record<string, unknown>> : [];
  const names = series.map(item => asString(item.name) || 'value');
  if (data.length === 0 || names.length === 0) return null;
  return blockCard(block, (
    <div className="h-44" aria-label={`${asString(block.title)}图表`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
          <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} />
          <Tooltip formatter={(value) => [value == null ? '数据受限' : formatNumber(Number(value)), '数值']} />
          {names.map((name, index) => (
            <Bar key={name} dataKey={name} fill={CHART_COLORS[index % CHART_COLORS.length]} radius={[4, 4, 0, 0]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  ), <BarChart2 className="h-4 w-4" />);
}

function LineChartBlock({ block }: { block: InsightBlock }) {
  const data = chartData(block);
  const series = Array.isArray(block.series) ? block.series as Array<Record<string, unknown>> : [];
  const names = series.map(item => asString(item.name) || 'value');
  if (data.length === 0 || names.length === 0) return null;
  return blockCard(block, (
    <div className="h-44" aria-label={`${asString(block.title)}图表`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
          <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={42} />
          <Tooltip formatter={(value) => [value == null ? '数据受限' : formatNumber(Number(value)), '数值']} />
          {names.map((name, index) => (
            <Line key={name} type="monotone" dataKey={name} stroke={CHART_COLORS[index % CHART_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  ), <Activity className="h-4 w-4" />);
}

function PieChartBlock({ block }: { block: InsightBlock }) {
  const categories = Array.isArray(block.categories) ? (block.categories as unknown[]).map(asString).filter(Boolean) : [];
  const series = Array.isArray(block.series) ? block.series as Array<Record<string, unknown>> : [];
  const first = series[0];
  const values = Array.isArray(first?.values) ? first.values as unknown[] : [];
  const data = categories.flatMap((name, index) => {
    const value = asNumber(values[index]);
    return value === null ? [] : [{ name, value }];
  });
  if (data.length === 0) return null;
  return blockCard(block, (
    <div className="flex items-center gap-2">
      <div className="h-[110px] w-[110px] shrink-0" aria-label={`${asString(block.title)}环形图`}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" innerRadius={28} outerRadius={48} paddingAngle={2} stroke="none">
              {data.map((item, index) => <Cell key={item.name} fill={CHART_COLORS[index % CHART_COLORS.length]} />)}
            </Pie>
            <Tooltip formatter={(value) => [formatNumber(Number(value)), '数值']} />
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
            <b className="text-slate-700">{formatNumber(item.value)}</b>
          </li>
        ))}
      </ul>
    </div>
  ), <PieChartIcon className="h-4 w-4" />);
}

function MarkdownBlock({ block }: { block: InsightBlock }) {
  const text = asString(block.text);
  if (!text.trim()) return null;
  return (
    <Card title={asString(block.title) || '说明'} icon={<FileText className="h-4 w-4" />}>
      <p className="whitespace-pre-wrap text-[12px] leading-5 text-slate-600">{text}</p>
    </Card>
  );
}

function TimelineBlock({ block }: { block: InsightBlock }) {
  const points = Array.isArray(block.points)
    ? (block.points as Array<Record<string, unknown>>).filter(point => point && typeof point === 'object' && (asString(point.title) || asString(point.date)))
    : [];
  if (points.length === 0) return null;
  return blockCard(block, (
    <ol className="space-y-2">
      {points.map((point, index) => (
        <li key={index} className="flex gap-2">
          <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-indigo-500" aria-hidden="true" />
          <div className="min-w-0">
            <p className="text-[12px] font-semibold text-slate-700">
              {asString(point.title)}
              {asString(point.date) && <span className="ml-1.5 text-[10px] font-normal text-slate-400">{asString(point.date)}</span>}
            </p>
            {asString(point.detail) && <p className="mt-0.5 text-[11px] leading-4 text-slate-500">{asString(point.detail)}</p>}
          </div>
        </li>
      ))}
    </ol>
  ), <List className="h-4 w-4" />);
}

function ReferencesBlock({ block }: { block: InsightBlock }) {
  const refs = Array.isArray(block.references)
    ? (block.references as Array<Record<string, unknown>>).filter(ref => ref && typeof ref === 'object' && (asString(ref.label) || asString(ref.url)))
    : [];
  if (refs.length === 0) return null;
  return blockCard(block, (
    <ul className="space-y-1.5">
      {refs.map((ref, index) => {
        // URL 白名单：非 http(s) 链接降级为纯文本，不注入可执行协议。
        const rawUrl = asString(ref.url);
        const url = safeHttpUrl(rawUrl);
        const label = asString(ref.label) || rawUrl;
        return (
          <li key={index} className="flex items-center gap-1.5 text-[11px]">
            <Link2 className="h-3 w-3 shrink-0 text-slate-300" aria-hidden="true" />
            {url ? (
              <a href={url} target="_blank" rel="noreferrer" className="truncate text-indigo-600 hover:underline">{label}</a>
            ) : (
              <span className="truncate text-slate-500">{label}</span>
            )}
          </li>
        );
      })}
    </ul>
  ), <Link2 className="h-4 w-4" />);
}

function BlockView({ block }: { block: InsightBlock }) {
  switch (block.block_type) {
    case 'metric_grid': return <MetricGridBlock block={block} />;
    case 'table': return <TableBlock block={block} />;
    case 'bar_chart': return <BarChartBlock block={block} />;
    case 'line_chart': return <LineChartBlock block={block} />;
    case 'pie_chart': return <PieChartBlock block={block} />;
    case 'markdown': return <MarkdownBlock block={block} />;
    case 'timeline': return <TimelineBlock block={block} />;
    case 'references': return <ReferencesBlock block={block} />;
    default: return null;
  }
}

export default function InsightBoardView({ payload }: { payload: InsightBoardPayload }) {
  const blocks = payload.data.filter(block => ALLOWED_BLOCK_TYPES.has(block.block_type));

  return (
    <div className="space-y-3">
      {payload.scope.summary && (
        <p className="rounded-lg bg-indigo-50/60 px-3 py-2 text-[11px] leading-4 text-indigo-700">{payload.scope.summary}</p>
      )}
      {blocks.map((block, index) => (
        <Fragment key={`${block.block_type}-${index}`}>
          <BlockView block={block} />
        </Fragment>
      ))}
      {payload.data.some(block => !ALLOWED_BLOCK_TYPES.has(block.block_type)) && (
        <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[10px] text-slate-400">存在不支持的钻取块类型，已忽略</p>
      )}
      {blocks.length === 0 && (
        <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-400">该钻取暂无可用分析块</p>
      )}
      {payload.narrative.summary && (
        <Card title="说明" icon={<List className="h-4 w-4" />}>
          <p className="whitespace-pre-wrap text-[11px] leading-4 text-slate-600">{payload.narrative.summary}</p>
        </Card>
      )}
    </div>
  );
}
