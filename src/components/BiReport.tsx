import {
  AlertTriangle, Award, BarChart2, CheckCircle2, CircleDollarSign, Database,
  HelpCircle, PieChart as PieChartIcon, Printer, ShieldCheck, Sparkles, Users,
} from 'lucide-react';
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { ReactNode } from 'react';

import type { ApiBiReport, ApiCandidate } from '../api/contracts';

interface BiReportProps {
  report?: ApiBiReport;
  candidateVersion?: number;
  selectedCandidates?: readonly ApiCandidate[];
}

const indigo = '#4f46e5';
const chartColors = ['#4f46e5', '#818cf8', '#a5b4fc', '#c7d2fe', '#0ea5e9', '#14b8a6'];

function MetricHelper({ title, formula, sampling }: { title: string; formula: string; sampling: string }) {
  return (
    <span className="group relative ml-1 inline-flex cursor-help items-center text-slate-300 transition hover:text-indigo-500">
      <HelpCircle className="h-3 w-3" />
      <span className="pointer-events-none absolute bottom-full left-0 z-50 mb-2 w-56 rounded-xl border border-slate-800 bg-slate-900/95 p-3 text-left text-[10px] font-normal leading-normal text-white opacity-0 shadow-xl transition-opacity group-hover:opacity-100">
        <span className="mb-1 block font-bold text-indigo-400">{title}</span>
        <code className="mb-2 block rounded border border-slate-800 bg-slate-950 px-1.5 py-1 text-[9px] text-slate-300">{formula}</code>
        <span className="block border-t border-slate-800 pt-1.5 text-[9px] text-slate-400">{sampling}</span>
      </span>
    </span>
  );
}

function Card({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm">
      <h3 className="mb-3 flex items-center gap-1.5 text-xs font-bold text-slate-700">
        <span className="text-indigo-500">{icon}</span>{title}
      </h3>
      {children}
    </section>
  );
}

function EmptyData({ label = '数据不足' }: { label?: string }) {
  return <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-400">{label}</p>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readText(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return undefined;
}

function readNumber(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return undefined;
}

function firstText(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const text = readText(record[key]);
    if (text) return text;
  }
  return undefined;
}

function firstNumber(record: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const number = readNumber(record[key]);
    if (number !== undefined) return number;
  }
  return undefined;
}

function entries(value: Record<string, unknown>): Array<{ name: string; value: number }> {
  return Object.entries(value).flatMap(([name, raw]) => {
    const numeric = readNumber(raw);
    return numeric === undefined ? [] : [{ name, value: numeric }];
  });
}

function formatMetric(value: unknown, suffix = ''): string {
  const numeric = readNumber(value);
  if (numeric === undefined) return '—';
  return `${numeric.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}${suffix}`;
}

function labelFor(key: string): string {
  const labels: Record<string, string> = {
    total_candidates: '候选人数', top_score: '最高总分', average_score: '平均总分',
    expected_reach: '预估触达', expected_engagement: '预估互动', budget: '预算',
    cpe: '互动成本', cpm: '千次曝光成本', audience_match: '受众匹配',
  };
  return labels[key] ?? key.replaceAll('_', ' ');
}

function displayRisk(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!isRecord(value)) return '风险数据不足';
  return firstText(value, ['label', 'reason', 'message', 'name', 'risk']) ?? '风险数据不足';
}

function displaySource(value: Record<string, unknown>): { name: string; collectedAt?: string; evidence?: string } {
  return {
    name: firstText(value, ['tool_name_cn', 'tool_name', 'source_name', 'name', 'label']) ?? '数据来源未标注',
    collectedAt: firstText(value, ['collected_at', 'generated_at', 'timestamp', 'time']),
    evidence: firstText(value, ['evidence_id', 'evidence_no', 'reference', 'id']),
  };
}

function OverviewCard({ overview }: { overview: Record<string, unknown> }) {
  const items = entries(overview).slice(0, 4);
  return (
    <Card title="任务概览" icon={<BarChart2 className="h-3.5 w-3.5" />}>
      {items.length === 0 ? <EmptyData /> : (
        <div className="grid grid-cols-2 gap-2">
          {items.map(item => (
            <div key={item.name} className="rounded-lg bg-slate-50 px-2.5 py-2">
              <p className="truncate text-[10px] font-medium text-slate-400">{labelFor(item.name)}</p>
              <p className="mt-0.5 text-sm font-bold text-slate-800">{formatMetric(item.value)}</p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ScoreCard({ data }: { data: Array<Record<string, unknown>> }) {
  const chartData = data.flatMap(item => {
    const name = firstText(item, ['name', 'dimension', 'label', 'metric']);
    const value = firstNumber(item, ['value', 'score', 'weight', 'percentage']);
    return name && value !== undefined ? [{ name, value }] : [];
  });
  return (
    <Card title="评分构成" icon={<Award className="h-3.5 w-3.5" />}>
      {chartData.length === 0 ? <EmptyData /> : (
        <div className="h-40" aria-label="评分构成图表">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} layout="vertical" margin={{ left: 6, right: 12 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="name" width={72} tick={{ fontSize: 10, fill: '#64748b' }} />
              <Tooltip formatter={(value) => [value, '得分']} />
              <Bar dataKey="value" fill={indigo} radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

function AudienceCard({ data }: { data: Record<string, unknown> }) {
  const indicators = entries(data).slice(0, 4);
  return (
    <Card title="受众与内容匹配" icon={<Users className="h-3.5 w-3.5" />}>
      {indicators.length === 0 ? <EmptyData label="受众数据不足" /> : (
        <div className="space-y-2">
          {indicators.map(item => (
            <div key={item.name}>
              <div className="mb-1 flex justify-between text-[10px] font-medium text-slate-500"><span>{labelFor(item.name)}</span><span>{formatMetric(item.value, '%')}</span></div>
              <div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-indigo-500" style={{ width: `${Math.min(100, Math.max(0, item.value))}%` }} /></div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function PlatformCard({ data }: { data: Array<Record<string, unknown>> }) {
  const chartData = data.flatMap(item => {
    const name = firstText(item, ['platform', 'name', 'label']);
    const value = firstNumber(item, ['value', 'count', 'percentage', 'score']);
    return name && value !== undefined ? [{ name, value }] : [];
  });
  return (
    <Card title="平台分布" icon={<PieChartIcon className="h-3.5 w-3.5" />}>
      {chartData.length === 0 ? <EmptyData /> : (
        <div className="flex items-center gap-2"><div className="h-32 w-32 shrink-0"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={chartData} dataKey="value" nameKey="name" innerRadius={28} outerRadius={46} paddingAngle={3}>{chartData.map((item, index) => <Cell key={item.name} fill={chartColors[index % chartColors.length]} />)}</Pie><Tooltip formatter={(value) => [value, '占比']} /></PieChart></ResponsiveContainer></div><div className="min-w-0 flex-1 space-y-1.5">{chartData.map((item, index) => <div key={item.name} className="flex items-center justify-between gap-2 text-[10px] text-slate-500"><span className="flex min-w-0 items-center gap-1.5 truncate"><i className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: chartColors[index % chartColors.length] }} />{item.name}</span><b className="text-slate-700">{formatMetric(item.value, '%')}</b></div>)}</div></div>
      )}
    </Card>
  );
}

function BudgetCard({ data }: { data: Record<string, unknown> }) {
  const items = entries(data).slice(0, 4);
  return (
    <Card title="预算与性价比" icon={<CircleDollarSign className="h-3.5 w-3.5" />}>
      {items.length === 0 ? <EmptyData /> : <div className="grid grid-cols-2 gap-2">{items.map(item => <div key={item.name} className="rounded-lg border border-indigo-50 bg-indigo-50/40 px-2.5 py-2"><p className="text-[10px] text-slate-500">{labelFor(item.name)}<MetricHelper title={labelFor(item.name)} formula="由已收录公开数据计算" sampling="仅展示本次报告可追溯的有效数据。" /></p><p className="mt-0.5 text-sm font-bold text-slate-800">{formatMetric(item.value)}</p></div>)}</div>}
    </Card>
  );
}

function ComparisonCard({ comparison, selectedCandidates }: { comparison: Array<Record<string, unknown>>; selectedCandidates: readonly ApiCandidate[] }) {
  const rows = comparison.flatMap(item => {
    const name = firstText(item, ['nickname', 'name', 'kol_name', 'label']);
    const score = firstNumber(item, ['total_score', 'score', 'value']);
    return name ? [{ name, score }] : [];
  });
  const candidates = selectedCandidates.slice(0, 4);
  return (
    <Card title="候选对比" icon={<CheckCircle2 className="h-3.5 w-3.5" />}>
      {candidates.length > 0 ? <div className="space-y-1.5">{candidates.map(candidate => <div key={candidate.id} className="flex items-center justify-between rounded-lg bg-slate-50 px-2.5 py-2 text-[11px]"><span className="truncate font-medium text-slate-700">#{candidate.rank} {candidate.nickname ?? candidate.kol_id}</span><span className="font-bold text-indigo-600">{formatMetric(candidate.total_score)}</span></div>)}</div> : rows.length > 0 ? <div className="space-y-1.5">{rows.map(row => <div key={row.name} className="flex justify-between rounded-lg bg-slate-50 px-2.5 py-2 text-[11px]"><span>{row.name}</span><b className="text-indigo-600">{formatMetric(row.score)}</b></div>)}</div> : <EmptyData label="尚未选择候选进行对比" />}
    </Card>
  );
}

function RiskCard({ risks }: { risks: Array<Record<string, unknown>> }) {
  return <Card title="风险与数据质量" icon={<ShieldCheck className="h-3.5 w-3.5" />}>{risks.length === 0 ? <EmptyData label="未发现需提示的风险；数据质量以来源证据为准" /> : <ul className="space-y-1.5">{risks.map((risk, index) => <li key={`${displayRisk(risk)}-${index}`} className="flex gap-1.5 rounded-lg bg-amber-50 px-2.5 py-2 text-[11px] text-amber-800"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{displayRisk(risk)}</li>)}</ul>}</Card>;
}

function SourceCard({ sources }: { sources: Array<Record<string, unknown>> }) {
  return <Card title="数据来源" icon={<Database className="h-3.5 w-3.5" />}>{sources.length === 0 ? <EmptyData label="暂无可展示的数据来源" /> : <ul className="space-y-2">{sources.map((source, index) => { const item = displaySource(source); return <li key={`${item.name}-${index}`} className="rounded-lg bg-slate-50 px-2.5 py-2 text-[10px] text-slate-500"><b className="block text-slate-700">{item.name}</b><span>{item.collectedAt ? `采集时间：${item.collectedAt}` : '采集时间未标注'}{item.evidence ? ` · 证据编号：${item.evidence}` : ''}</span></li>; })}</ul>}</Card>;
}

function PanelState({ children }: { children: ReactNode }) {
  return <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]"><header className="flex h-14 items-center border-b border-slate-200 px-4"><h2 className="text-xs font-bold uppercase tracking-widest text-slate-800">BI Intelligence Output</h2></header><div className="flex flex-1 items-center justify-center bg-slate-50/40 p-8 text-center text-xs text-slate-500">{children}</div></aside>;
}

export default function BiReport({ report, candidateVersion, selectedCandidates = [] }: BiReportProps) {
  if (!report) return <PanelState>等待生成 KOL 决策报告</PanelState>;
  if (candidateVersion === undefined || report.candidate_version !== candidateVersion) return <PanelState>正在同步最新候选与 BI 报告</PanelState>;

  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm print-container xl:w-[420px]">
      <header className="flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4 shrink-0">
        <div><h2 className="text-xs font-bold uppercase tracking-widest text-slate-800">BI Intelligence Output</h2><p className="mt-0.5 text-[9px] text-slate-400">报告 v{report.report_version} · {new Date(report.generated_at).toLocaleString('zh-CN')}</p></div>
        <button onClick={() => window.print()} className="no-print flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-[10px] font-bold text-white shadow-sm transition hover:bg-indigo-700" title="导出 PDF KOL 决策报告"><Printer className="h-3.5 w-3.5" />导出 PDF</button>
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto bg-slate-50/40 p-3 print-scrollable">
        <OverviewCard overview={report.overview} />
        <ScoreCard data={report.score_composition} />
        <AudienceCard data={report.audience_content_fit} />
        <PlatformCard data={report.platform_distribution} />
        <BudgetCard data={report.budget_analysis} />
        <ComparisonCard comparison={report.comparison} selectedCandidates={selectedCandidates} />
        <RiskCard risks={report.risks} />
        <Card title="AI 结论" icon={<Sparkles className="h-3.5 w-3.5" />}><p className="whitespace-pre-wrap text-[11px] leading-5 text-slate-600">{report.conclusion || '暂未生成 AI 结论'}</p></Card>
        <SourceCard sources={report.sources} />
      </div>
    </aside>
  );
}
