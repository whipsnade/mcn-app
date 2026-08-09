import { useEffect, useState } from 'react';
import { createAdminTenant, listAdminTenants, type AdminTenant } from '../../api/adminGateway';

export default function TenantAdmin() {
  const [items, setItems] = useState<AdminTenant[]>([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  useEffect(() => { void listAdminTenants().then(value => setItems(value.items)).catch(err => setError(err instanceof Error ? err.message : '加载失败')).finally(() => setLoading(false)); }, []);
  const create = async () => {
    const name = window.prompt('租户名称'); if (!name) return; const slug = window.prompt('租户 slug'); if (!slug) return;
    if (!window.confirm('确认创建租户？')) return;
    try { const tenant = await createAdminTenant({ name, slug }); setItems(value => [tenant, ...value]); } catch (err) { setError(err instanceof Error ? err.message : '创建失败'); }
  };
  return <section aria-labelledby="tenant-admin-title" className="space-y-4 p-5"><div className="flex items-center justify-between"><h3 id="tenant-admin-title" className="text-sm font-bold">租户</h3><button type="button" onClick={() => void create()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white">新建租户</button></div>{loading && <p role="status">加载中…</p>}{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="overflow-x-auto"><table className="w-full text-left text-xs"><caption className="sr-only">租户列表</caption><thead><tr className="text-slate-400"><th className="p-2">名称</th><th className="p-2">Backend</th><th className="p-2">状态</th><th className="p-2">活动 Run</th></tr></thead><tbody>{items.map(item => <tr key={item.id} className="border-t border-slate-100"><td className="p-2 font-bold">{item.name}</td><td className="p-2">{item.runtime_backend}</td><td className="p-2">{item.status}</td><td className="p-2">{item.active_run_count}</td></tr>)}</tbody></table></div></section>;
}
