import { useState } from 'react';
import { getAdminRunDiagnostics, type AdminRunDiagnostics } from '../../api/adminGateway';

export default function RunDiagnostics() {
  const [runId, setRunId] = useState(''); const [data, setData] = useState<AdminRunDiagnostics | null>(null); const [error, setError] = useState('');
  const load = async () => { if (!runId.trim()) return; setError(''); try { setData(await getAdminRunDiagnostics(runId.trim())); } catch (err) { setError(err instanceof Error ? err.message : '加载失败'); } };
  return <section aria-labelledby="run-diagnostics-title" className="space-y-4 p-5"><h3 id="run-diagnostics-title" className="text-sm font-bold">Run 诊断（只读）</h3><div className="flex gap-2"><label htmlFor="diagnostic-run-id" className="sr-only">Run ID</label><input id="diagnostic-run-id" value={runId} onChange={event => setRunId(event.target.value)} placeholder="输入 Run ID" className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-xs" /><button type="button" onClick={() => void load()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white">查询</button></div>{error && <p role="alert" className="text-rose-600">{error}</p>}{data && <div className="grid gap-3 text-xs md:grid-cols-2"><div className="rounded-xl bg-slate-50 p-3"><strong>{String(data.run.id)}</strong><p>{String(data.run.status)} · {String(data.run.runtime_backend)}</p></div><div className="rounded-xl bg-slate-50 p-3"><p>Attempts {data.attempts.length}</p><p>Events {data.events.length}</p><p>Usage {data.usage.length}</p></div></div>}</section>;
}
