import { useEffect, useState } from 'react';
import { createAdminTenant, listAdminTenants, updateAdminTenant, type AdminTenant } from '../../api/adminGateway';

export default function TenantAdmin() {
  const [items, setItems] = useState<AdminTenant[]>([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [saving, setSaving] = useState<string | null>(null); const [draftBackend, setDraftBackend] = useState<Record<string, 'current' | 'pi'>>({});
  useEffect(() => { void listAdminTenants().then(value => setItems(value.items)).catch(err => setError(err instanceof Error ? err.message : '加载失败')).finally(() => setLoading(false)); }, []);
  const create = async () => {
    const name = window.prompt('租户名称'); if (!name) return; const slug = window.prompt('租户 slug'); if (!slug) return;
    if (!window.confirm('确认创建租户？')) return;
    try { const tenant = await createAdminTenant({ name, slug }); setItems(value => [tenant, ...value]); } catch (err) { setError(err instanceof Error ? err.message : '创建失败'); }
  };
  const changeBackend = async (item: AdminTenant, backend: 'current' | 'pi') => {
    if (backend === item.runtime_backend) return;
    if (!window.confirm(`确认将 ${item.name} 的新 Run 切换到 ${backend}？在途 Run 不受影响。`)) return;
    setSaving(item.id); setError('');
    try {
      const updated = await updateAdminTenant(item.id, { runtime_backend: backend });
      setItems(value => value.map(entry => entry.id === updated.id ? updated : entry));
      setDraftBackend(value => { const next = { ...value }; delete next[item.id]; return next; });
    } catch (err) { setError(err instanceof Error ? err.message : 'Backend 切换失败'); }
    finally { setSaving(null); }
  };
  return <section aria-labelledby="tenant-admin-title" className="space-y-4 p-5"><div className="flex items-center justify-between"><h3 id="tenant-admin-title" className="text-sm font-bold">租户</h3><button type="button" onClick={() => void create()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white">新建租户</button></div>{loading && <p role="status">加载中…</p>}{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="overflow-x-auto"><table className="w-full text-left text-xs"><caption className="sr-only">租户列表</caption><thead><tr className="text-slate-400"><th className="p-2">名称</th><th className="p-2">Backend</th><th className="p-2">状态</th><th className="p-2">活动 Run</th></tr></thead><tbody>{items.map(item => { const backend = draftBackend[item.id] ?? item.runtime_backend; return <tr key={item.id} className="border-t border-slate-100"><td className="p-2 font-bold">{item.name}</td><td className="p-2"><label htmlFor={`tenant-backend-${item.id}`} className="sr-only">{item.name} Backend</label><select id={`tenant-backend-${item.id}`} value={backend} disabled={saving === item.id} onChange={event => setDraftBackend(value => ({ ...value, [item.id]: event.target.value as 'current' | 'pi' }))}><option value="current">current</option><option value="pi">pi</option></select><button type="button" className="ml-2 rounded border px-2 py-1" disabled={saving === item.id || backend === item.runtime_backend} onClick={() => void changeBackend(item, backend)}>应用 Backend</button></td><td className="p-2">{item.status}</td><td className="p-2">{item.active_run_count}</td></tr>; })}</tbody></table></div></section>;
}
