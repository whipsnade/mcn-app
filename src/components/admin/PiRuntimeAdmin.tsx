import { useEffect, useState } from 'react';

import { listAdminGateways, updateAdminGateway, type AdminGateway } from '../../api/adminGateway';
import ConfirmDialog from './ConfirmDialog';

type PendingAction =
  | { kind: 'mode'; gateway: AdminGateway; mode: 'active' | 'draining' }
  | { kind: 'capacity'; gateway: AdminGateway; capacity: number };

const STATUS_LABEL: Record<AdminGateway['status'], string> = {
  active: '在线',
  offline: '离线',
  disabled: '已禁用',
};

const MODE_LABEL: Record<AdminGateway['mode'], string> = {
  active: '接收新任务',
  draining: '排空中',
};

export default function PiRuntimeAdmin() {
  const [items, setItems] = useState<AdminGateway[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [draftCapacity, setDraftCapacity] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<PendingAction | null>(null);

  useEffect(() => {
    void listAdminGateways()
      .then(value => setItems(value.items))
      .catch(err => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const confirmAction = async () => {
    if (!pending) return;
    setSaving(true);
    setError('');
    try {
      const input = pending.kind === 'mode'
        ? { mode: pending.mode }
        : { desired_capacity: pending.capacity };
      const next = await updateAdminGateway(pending.gateway.gateway_id, input);
      setItems(value => value.map(row => (row.gateway_id === next.gateway_id ? next : row)));
      setDraftCapacity(value => {
        const rest = { ...value };
        delete rest[pending.gateway.gateway_id];
        return rest;
      });
      setPending(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-labelledby="pi-runtime-admin-title" className="space-y-4 p-5">
      <h3 id="pi-runtime-admin-title" className="text-sm font-bold">Pi Runtime</h3>
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {!loading && !error && items.length === 0 && <p className="text-xs text-slate-400">暂无 Gateway 实例</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">Pi Gateway 实例列表</caption>
            <thead>
              <tr className="text-slate-400">
                <th className="p-2">Gateway</th>
                <th className="p-2">状态</th>
                <th className="p-2">模式</th>
                <th className="p-2">期望容量</th>
                <th className="p-2">最近上报</th>
                <th className="p-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const draft = draftCapacity[item.gateway_id] ?? String(item.desired_capacity);
                const capacity = Number(draft);
                const capacityChanged = Number.isInteger(capacity) && capacity >= 1 && capacity <= 128 && capacity !== item.desired_capacity;
                const targetMode = item.mode === 'draining' ? 'active' : 'draining';
                return (
                  <tr key={item.gateway_id} className="border-t border-slate-100">
                    <td className="p-2 font-bold">{item.gateway_id}</td>
                    <td className="p-2">
                      <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${
                        item.status === 'active' ? 'bg-emerald-50 text-emerald-700'
                          : item.status === 'offline' ? 'bg-slate-100 text-slate-500'
                            : 'bg-rose-50 text-rose-600'
                      }`}
                      >
                        <span aria-hidden="true">{item.status === 'active' ? '●' : item.status === 'offline' ? '○' : '×'}</span>
                        {STATUS_LABEL[item.status]}
                      </span>
                    </td>
                    <td className="p-2">
                      <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${item.mode === 'draining' ? 'bg-amber-50 text-amber-700' : 'bg-indigo-50 text-indigo-700'}`}>
                        <span aria-hidden="true">{item.mode === 'draining' ? '◐' : '▶'}</span>
                        {MODE_LABEL[item.mode]}
                      </span>
                    </td>
                    <td className="p-2">
                      <label htmlFor={`gateway-capacity-${item.gateway_id}`} className="sr-only">{item.gateway_id} 期望容量</label>
                      <input
                        id={`gateway-capacity-${item.gateway_id}`}
                        type="number"
                        min={1}
                        max={128}
                        value={draft}
                        disabled={saving}
                        onChange={event => setDraftCapacity(value => ({ ...value, [item.gateway_id]: event.target.value }))}
                        className="w-20 rounded-lg border border-slate-200 px-2 py-1"
                      />
                      <button
                        type="button"
                        className="ml-2 rounded border border-slate-200 px-2 py-1 disabled:opacity-50"
                        disabled={saving || !capacityChanged}
                        onClick={() => setPending({ kind: 'capacity', gateway: item, capacity })}
                      >
                        应用容量
                      </button>
                    </td>
                    <td className="p-2">{item.last_seen_at ? item.last_seen_at.slice(0, 16).replace('T', ' ') : '从未上报'}</td>
                    <td className="p-2">
                      <button
                        type="button"
                        className="rounded border border-slate-200 px-2 py-1"
                        disabled={saving}
                        onClick={() => setPending({ kind: 'mode', gateway: item, mode: targetMode })}
                      >
                        {targetMode === 'draining' ? '切换为排空' : '切换为接收'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={pending?.kind === 'capacity' ? '调整期望容量' : '切换运行模式'}
        description={
          pending?.kind === 'capacity'
            ? `确认将 ${pending.gateway.gateway_id} 的期望容量调整为 ${pending.capacity}？新容量只影响后续调度。`
            : pending?.mode === 'draining'
              ? `确认将 ${pending.gateway.gateway_id} 切换为排空（draining）？排空只阻止新 claim，在途 Worker 自然完成，不会被中断。`
              : pending
                ? `确认将 ${pending.gateway.gateway_id} 切换为接收（active）？恢复后它将重新参与新任务调度。`
                : ''
        }
        confirmLabel={pending?.kind === 'capacity' ? '确认调整' : '确认切换'}
        busy={saving}
        onConfirm={() => void confirmAction()}
        onCancel={() => setPending(null)}
      />
    </section>
  );
}
