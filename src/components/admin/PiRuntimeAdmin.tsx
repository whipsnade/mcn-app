import { useEffect, useState } from 'react';
import { listAdminGateways, updateAdminGateway, type AdminGateway } from '../../api/adminGateway';

export default function PiRuntimeAdmin() {
  const [items, setItems] = useState<AdminGateway[]>([]); const [error, setError] = useState('');
  useEffect(() => { void listAdminGateways().then(value => setItems(value.items)).catch(err => setError(err instanceof Error ? err.message : '加载失败')); }, []);
  const toggle = async (item: AdminGateway) => { if (!window.confirm(`确认${item.mode === 'draining' ? '恢复 active' : '进入 draining'}？只影响新 Run`)) return; try { const next = await updateAdminGateway(item.gateway_id, { mode: item.mode === 'draining' ? 'active' : 'draining' }); setItems(value => value.map(row => row.gateway_id === item.gateway_id ? next : row)); } catch (err) { setError(err instanceof Error ? err.message : '更新失败'); } };
  return <section aria-labelledby="pi-runtime-admin-title" className="space-y-4 p-5"><h3 id="pi-runtime-admin-title" className="text-sm font-bold">Pi Runtime</h3>{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="space-y-2">{items.map(item => <article key={item.gateway_id} className="flex items-center justify-between rounded-xl border border-slate-100 p-3 text-xs"><div><strong>{item.gateway_id}</strong><p className="text-slate-500">{item.status} · capacity {item.desired_capacity}</p></div><button type="button" onClick={() => void toggle(item)} className="rounded-lg border border-slate-200 px-2 py-1">{item.mode}</button></article>)}</div></section>;
}
