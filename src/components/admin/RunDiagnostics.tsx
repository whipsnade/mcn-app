import { useState } from 'react';

import { getAdminRunDiagnostics, type AdminRunDiagnostics } from '../../api/adminGateway';

type TimelineBadge = { text: string; tone: 'warn' | 'info' };

type TimelineEntry = {
  id: string;
  time: string | null;
  kind: '尝试' | '步骤' | '工具调用' | '事件' | '用量';
  title: string;
  detail: string;
  badges: TimelineBadge[];
};

const text = (value: unknown): string => (value == null || value === '' ? '—' : String(value));

const formatTime = (value: unknown): string => (typeof value === 'string' && value ? value.slice(0, 19).replace('T', ' ') : '—');

// 只提取后端安全投影里的已知标量字段，绝不渲染 raw JSON 原文块。
function buildTimeline(data: AdminRunDiagnostics): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  data.attempts.forEach(attempt => {
    entries.push({
      id: `attempt-${text(attempt.id)}`,
      time: typeof attempt.started_at === 'string' ? attempt.started_at : null,
      kind: '尝试',
      title: `尝试 #${text(attempt.attempt)}`,
      detail: `结果 ${text(attempt.outcome)} · 结束 ${formatTime(attempt.ended_at)}`,
      badges: [],
    });
  });
  data.steps.forEach(step => {
    entries.push({
      id: `step-${text(step.id)}`,
      time: typeof step.created_at === 'string' ? step.created_at : null,
      kind: '步骤',
      title: text(step.step_type),
      detail: `序号 ${text(step.sequence)} · ${text(step.status)} · ${text(step.duration_ms)} ms`,
      badges: [],
    });
  });
  data.tool_calls.forEach(call => {
    const badges: TimelineBadge[] = [];
    if (call.status === 'unknown') badges.push({ text: 'unknown', tone: 'warn' });
    if (typeof call.points_reserved === 'number' && call.points_reserved > 0) badges.push({ text: '预留中', tone: 'warn' });
    entries.push({
      id: `call-${text(call.id)}`,
      time: typeof call.completed_at === 'string' ? call.completed_at : null,
      kind: '工具调用',
      title: text(call.internal_tool_name),
      detail: `${text(call.service)} · ${text(call.status)} · 预留 ${text(call.points_reserved)} · 结算 ${text(call.points_settled)}${call.error_type ? ` · 错误 ${text(call.error_type)}` : ''}`,
      badges,
    });
  });
  data.events.forEach(event => {
    entries.push({
      id: `event-${text(event.id)}`,
      time: typeof event.created_at === 'string' ? event.created_at : null,
      kind: '事件',
      title: text(event.event_type),
      detail: `序号 ${text(event.sequence)}`,
      badges: [],
    });
  });
  data.usage.forEach(usage => {
    entries.push({
      id: `usage-${text(usage.id)}`,
      time: typeof usage.observed_at === 'string' ? usage.observed_at : null,
      kind: '用量',
      title: text(usage.model ?? usage.kind),
      detail: `输入 ${text(usage.input_tokens)} · 输出 ${text(usage.output_tokens)} · 成本 ${text(usage.cost_micros)} micros · ${text(usage.cost_status)}`,
      badges: usage.usage_status === 'unavailable' ? [{ text: '用量缺失', tone: 'warn' }] : [],
    });
  });
  return entries.sort((a, b) => {
    if (a.time === null && b.time === null) return 0;
    if (a.time === null) return 1;
    if (b.time === null) return -1;
    return a.time.localeCompare(b.time);
  });
}

function ReconciliationSummary({ value }: { value: Record<string, unknown> }) {
  const scalars = Object.entries(value).filter(([, item]) =>
    item == null || typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean' || Array.isArray(item));
  return (
    <div className="rounded-xl bg-slate-50 p-3 text-xs">
      <h4 className="font-bold">对账结果</h4>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-3">
        {scalars.map(([name, item]) => (
          <div key={name}>
            <dt className="inline text-slate-400">{name}: </dt>
            <dd className={`inline font-bold ${name === 'reconciliation_status' && item === 'mismatch' ? 'text-rose-600' : 'text-slate-700'}`}>
              {Array.isArray(item) ? item.map(String).join('、') || '—' : text(item)}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default function RunDiagnostics() {
  const [runId, setRunId] = useState('');
  const [data, setData] = useState<AdminRunDiagnostics | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError('');
    try {
      setData(await getAdminRunDiagnostics(id));
    } catch (err) {
      setData(null);
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  const timeline = data ? buildTimeline(data) : [];

  return (
    <section aria-labelledby="run-diagnostics-title" className="space-y-4 p-5">
      <h3 id="run-diagnostics-title" className="text-sm font-bold">Run 诊断（只读）</h3>
      <div className="flex gap-2">
        <label htmlFor="diagnostic-run-id" className="sr-only">Run ID</label>
        <input
          id="diagnostic-run-id"
          value={runId}
          onChange={event => setRunId(event.target.value)}
          placeholder="输入 Run ID"
          className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-xs"
        />
        <button
          type="button"
          disabled={!runId.trim() || loading}
          onClick={() => void load()}
          className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white disabled:opacity-50"
        >
          查询
        </button>
      </div>
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {data && !loading && (
        <div className="space-y-3 text-xs">
          <div className="rounded-xl bg-slate-50 p-3">
            <strong>{text(data.run.id)}</strong>
            <p className="mt-1 text-slate-500">
              {`状态 ${text(data.run.status)} · 结果 ${text(data.run.outcome)} · Backend ${text(data.run.runtime_backend)} · 错误码 ${text(data.run.error_code)}`}
            </p>
            <p className="mt-1 text-slate-400">
              {`创建 ${formatTime(data.run.created_at)} · 开始 ${formatTime(data.run.started_at)} · 完成 ${formatTime(data.run.completed_at)}`}
            </p>
          </div>
          {data.reconciliation && <ReconciliationSummary value={data.reconciliation} />}
          <ol aria-label="Run 时间线" className="space-y-2">
            {timeline.map(entry => (
              <li key={entry.id} className="rounded-xl border border-slate-100 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-bold text-indigo-700">{entry.kind}</span>
                  <strong>{entry.title}</strong>
                  {entry.badges.map(badge => (
                    <span
                      key={badge.text}
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${badge.tone === 'warn' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-500'}`}
                    >
                      <span aria-hidden="true">⚠</span>
                      {badge.text}
                    </span>
                  ))}
                  <span className="ml-auto text-slate-400">{formatTime(entry.time)}</span>
                </div>
                <p className="mt-1 text-slate-500">{entry.detail}</p>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
