import { useEffect, useState } from 'react';

import { createAdminTenant, listAdminTenants, updateAdminTenant, type AdminTenant } from '../../api/adminGateway';
import ConfirmDialog from './ConfirmDialog';

type PendingBackend = { tenant: AdminTenant; backend: 'current' | 'pi' };

export default function TenantAdmin() {
  const [items, setItems] = useState<AdminTenant[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [draftBackend, setDraftBackend] = useState<Record<string, 'current' | 'pi'>>({});
  const [pendingBackend, setPendingBackend] = useState<PendingBackend | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [draftName, setDraftName] = useState('');
  const [draftSlug, setDraftSlug] = useState('');
  const [draftInternal, setDraftInternal] = useState(false);

  useEffect(() => {
    void listAdminTenants()
      .then(value => setItems(value.items))
      .catch(err => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const submitCreate = async () => {
    setSaving(true);
    setError('');
    try {
      const tenant = await createAdminTenant({ name: draftName.trim(), slug: draftSlug.trim(), is_internal: draftInternal });
      setItems(value => [tenant, ...value]);
      setCreateOpen(false);
      setDraftName('');
      setDraftSlug('');
      setDraftInternal(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败');
    } finally {
      setSaving(false);
    }
  };

  const confirmBackend = async () => {
    if (!pendingBackend) return;
    const { tenant, backend } = pendingBackend;
    setSaving(true);
    setError('');
    try {
      const updated = await updateAdminTenant(tenant.id, { runtime_backend: backend });
      setItems(value => value.map(entry => (entry.id === updated.id ? updated : entry)));
      setDraftBackend(value => {
        const next = { ...value };
        delete next[tenant.id];
        return next;
      });
      setPendingBackend(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backend 切换失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-labelledby="tenant-admin-title" className="space-y-4 p-5">
      <div className="flex items-center justify-between">
        <h3 id="tenant-admin-title" className="text-sm font-bold">租户</h3>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white"
        >
          新建租户
        </button>
      </div>
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {!loading && !error && items.length === 0 && <p className="text-xs text-slate-400">暂无租户</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">租户列表</caption>
          <thead>
            <tr className="text-slate-400">
              <th className="p-2">名称</th>
              <th className="p-2">Backend</th>
              <th className="p-2">状态</th>
              <th className="p-2">活动 Run</th>
            </tr>
          </thead>
          <tbody>
            {items.map(item => {
              const backend = draftBackend[item.id] ?? item.runtime_backend;
              return (
                <tr key={item.id} className="border-t border-slate-100">
                  <td className="p-2 font-bold">{item.name}</td>
                  <td className="p-2">
                    <label htmlFor={`tenant-backend-${item.id}`} className="sr-only">{item.name} Backend</label>
                    <select
                      id={`tenant-backend-${item.id}`}
                      value={backend}
                      disabled={saving}
                      onChange={event => setDraftBackend(value => ({ ...value, [item.id]: event.target.value as 'current' | 'pi' }))}
                    >
                      <option value="current">current</option>
                      <option value="pi">pi</option>
                    </select>
                    <button
                      type="button"
                      className="ml-2 rounded border px-2 py-1"
                      disabled={saving || backend === item.runtime_backend}
                      onClick={() => setPendingBackend({ tenant: item, backend })}
                    >
                      应用 Backend
                    </button>
                  </td>
                  <td className="p-2">{item.status}</td>
                  <td className="p-2">{item.active_run_count}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={createOpen}
        title="新建租户"
        confirmLabel="创建"
        busy={saving}
        confirmDisabled={!draftName.trim() || !draftSlug.trim()}
        onConfirm={() => void submitCreate()}
        onCancel={() => setCreateOpen(false)}
      >
        <div className="mt-3 space-y-3">
          <div>
            <label htmlFor="tenant-create-name" className="mb-1 block text-xs font-bold text-slate-600">租户名称</label>
            <input
              id="tenant-create-name"
              value={draftName}
              onChange={event => setDraftName(event.target.value)}
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
            />
          </div>
          <div>
            <label htmlFor="tenant-create-slug" className="mb-1 block text-xs font-bold text-slate-600">租户 slug</label>
            <input
              id="tenant-create-slug"
              value={draftSlug}
              onChange={event => setDraftSlug(event.target.value)}
              placeholder="小写字母数字与连字符"
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
            />
          </div>
          <label htmlFor="tenant-create-internal" className="flex items-center gap-2 text-xs text-slate-600">
            <input
              id="tenant-create-internal"
              type="checkbox"
              checked={draftInternal}
              onChange={event => setDraftInternal(event.target.checked)}
            />
            内部租户
          </label>
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={pendingBackend !== null}
        title="切换 Runtime Backend"
        description={pendingBackend ? `确认将 ${pendingBackend.tenant.name} 的新 Run 切换到 ${pendingBackend.backend}？此变更只影响新 Run，在途 Run 与历史 snapshot 不变。` : ''}
        confirmLabel="确认切换"
        busy={saving}
        onConfirm={() => void confirmBackend()}
        onCancel={() => setPendingBackend(null)}
      />
    </section>
  );
}
