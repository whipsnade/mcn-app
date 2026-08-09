import { useEffect, useState } from 'react';
import { listAdminUsage, type AdminUsage } from '../../api/adminGateway';

export default function UsageAdmin({ tenantId }: { tenantId?: string }) {
  const [items, setItems] = useState<AdminUsage[]>([]); const [error, setError] = useState('');
  useEffect(() => { if (!tenantId) return; void listAdminUsage(tenantId).then(value => setItems(value.items)).catch(err => setError(err instanceof Error ? err.message : '加载失败')); }, [tenantId]);
  return <section aria-labelledby="usage-admin-title" className="space-y-4 p-5"><h3 id="usage-admin-title" className="text-sm font-bold">用量与积分</h3>{!tenantId && <p className="text-xs text-slate-400">请先选择租户</p>}{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="grid gap-3 md:grid-cols-3">{items.map((item, index) => <div key={`${item.day ?? item.run_id ?? 'item'}-${index}`} className="rounded-xl bg-slate-50 p-3 text-xs"><p className="font-bold">{item.day ?? item.user_id ?? 'tenant'}</p><p className="mt-1 text-slate-500">tokens {item.input_tokens ?? 0} / {item.output_tokens ?? 0}</p><p className="mt-1 text-indigo-600">{item.cost_micros == null ? 'unpriced' : `${item.cost_micros} micros`}</p></div>)}</div></section>;
}
