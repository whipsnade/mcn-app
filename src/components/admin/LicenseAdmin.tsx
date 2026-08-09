import { useEffect, useState } from 'react';
import { listAdminLicenses, type AdminLicense } from '../../api/adminGateway';

export default function LicenseAdmin({ tenantId }: { tenantId?: string }) {
  const [items, setItems] = useState<AdminLicense[]>([]); const [error, setError] = useState('');
  useEffect(() => { if (!tenantId) return; void listAdminLicenses(tenantId).then(setItems).catch(err => setError(err instanceof Error ? err.message : '加载失败')); }, [tenantId]);
  return <section aria-labelledby="license-admin-title" className="space-y-4 p-5"><h3 id="license-admin-title" className="text-sm font-bold">License</h3>{!tenantId && <p className="text-xs text-slate-400">请先选择租户</p>}{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="space-y-2">{items.map(item => <article key={item.id} className="rounded-xl border border-slate-100 p-3 text-xs"><div className="flex justify-between"><strong>v{item.version}</strong><span>{item.active ? 'active' : '历史'}</span></div><p className="mt-1 text-slate-500">并发 {item.max_concurrent_runs} / 用户 {item.max_user_concurrent_runs}</p></article>)}</div></section>;
}
