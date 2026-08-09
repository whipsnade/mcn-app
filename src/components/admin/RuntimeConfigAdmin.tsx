import { useEffect, useState } from 'react';
import { listAdminRuntimeConfigs, type AdminRuntimeConfig } from '../../api/adminGateway';

export default function RuntimeConfigAdmin({ tenantId }: { tenantId?: string }) {
  const [items, setItems] = useState<AdminRuntimeConfig[]>([]); const [error, setError] = useState('');
  useEffect(() => { if (!tenantId) return; void listAdminRuntimeConfigs(tenantId).then(value => setItems(value.items)).catch(err => setError(err instanceof Error ? err.message : '加载失败')); }, [tenantId]);
  return <section aria-labelledby="runtime-config-admin-title" className="space-y-4 p-5"><h3 id="runtime-config-admin-title" className="text-sm font-bold">Runtime 配置</h3><p className="text-xs text-slate-500">secret 为 write-only；页面不会读取旧明文。</p>{error && <p role="alert" className="text-rose-600">{error}</p>}<div className="space-y-2">{items.map(item => <article key={item.id} className="rounded-xl border border-slate-100 p-3 text-xs"><div className="flex justify-between"><strong>v{item.version} · {item.runtime_backend}</strong><span>{item.status}</span></div><p className="mt-1 text-slate-500">{item.secret_refs.length ? `${item.secret_refs.length} 个 secret 已加密` : '无 secret'}</p></article>)}</div></section>;
}
