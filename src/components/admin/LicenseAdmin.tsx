import { useCallback, useEffect, useState } from 'react';

import {
  createAdminLicense,
  listAdminLicenses,
  updateAdminLicense,
  type AdminLicense,
} from '../../api/adminGateway';
import ConfirmDialog from './ConfirmDialog';
import TenantSelect from './TenantSelect';

// 与后端 SUPPORTED_LICENSE_FEATURES 对齐的功能开关集合。
const FEATURE_KEYS = ['kol_selection', 'brand_analysis', 'campaign_analysis', 'kol_detail', 'utility'] as const;

type PendingStatus = { license: AdminLicense; status: 'active' | 'suspended' };

const formatDay = (value: string | null): string => (value ? value.slice(0, 10) : '永久有效');

export default function LicenseAdmin({ tenantId: initialTenantId }: { tenantId?: string }) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? '');
  const [items, setItems] = useState<AdminLicense[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [draftFeatures, setDraftFeatures] = useState<Record<string, boolean>>({});
  const [draftMaxRuns, setDraftMaxRuns] = useState('5');
  const [draftMaxUserRuns, setDraftMaxUserRuns] = useState('2');
  const [draftValidFrom, setDraftValidFrom] = useState('');
  const [draftValidUntil, setDraftValidUntil] = useState('');
  const [pendingStatus, setPendingStatus] = useState<PendingStatus | null>(null);

  const reload = useCallback(async (id: string) => {
    setLoading(true);
    setError('');
    try {
      setItems(await listAdminLicenses(id));
    } catch (err) {
      setItems([]);
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!tenantId) {
      setItems([]);
      return;
    }
    void reload(tenantId);
  }, [tenantId, reload]);

  const submitCreate = async () => {
    if (!tenantId) return;
    setSaving(true);
    setError('');
    try {
      const features: Record<string, boolean> = Object.fromEntries(
        Object.entries(draftFeatures).filter((entry): entry is [string, boolean] => entry[1] === true),
      );
      await createAdminLicense(tenantId, {
        features,
        max_concurrent_runs: Number(draftMaxRuns),
        max_user_concurrent_runs: Number(draftMaxUserRuns),
        ...(draftValidFrom ? { valid_from: new Date(draftValidFrom).toISOString() } : {}),
        ...(draftValidUntil ? { valid_until: new Date(draftValidUntil).toISOString() } : {}),
      }).then(created => setItems(value => [...value, created]));
      setCreateOpen(false);
      setDraftFeatures({});
      setDraftValidFrom('');
      setDraftValidUntil('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败');
    } finally {
      setSaving(false);
    }
  };

  const confirmStatus = async () => {
    if (!pendingStatus || !tenantId) return;
    setSaving(true);
    setError('');
    try {
      await updateAdminLicense(tenantId, pendingStatus.license.id, pendingStatus.status);
      setPendingStatus(null);
      await reload(tenantId);
    } catch (err) {
      setError(err instanceof Error ? err.message : '状态更新失败');
    } finally {
      setSaving(false);
    }
  };

  const maxRuns = Number(draftMaxRuns);
  const maxUserRuns = Number(draftMaxUserRuns);
  const createValid =
    Object.values(draftFeatures).some(Boolean) &&
    Number.isInteger(maxRuns) && maxRuns >= 1 && maxRuns <= 1000 &&
    Number.isInteger(maxUserRuns) && maxUserRuns >= 1 && maxUserRuns <= 1000;

  return (
    <section aria-labelledby="license-admin-title" className="space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="license-admin-title" className="text-sm font-bold">License</h3>
        <TenantSelect value={tenantId} onChange={setTenantId} />
      </div>
      {!tenantId && <p className="text-xs text-slate-400">请先选择租户</p>}
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {tenantId && !loading && !error && (
        <>
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white"
            >
              追加新版本
            </button>
          </div>
          {items.length === 0 && <p className="text-xs text-slate-400">暂无 License 版本</p>}
          {items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <caption className="sr-only">License 版本列表</caption>
                <thead>
                  <tr className="text-slate-400">
                    <th className="p-2">版本</th>
                    <th className="p-2">有效期</th>
                    <th className="p-2">功能</th>
                    <th className="p-2">并发上限</th>
                    <th className="p-2">状态</th>
                    <th className="p-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(item => {
                    const enabled = Object.entries(item.features).filter(([, value]) => value).map(([name]) => name);
                    return (
                      <tr key={item.id} className="border-t border-slate-100">
                        <td className="p-2 font-bold">v{item.version}</td>
                        <td className="p-2"><span>{formatDay(item.valid_from)}</span>{' ~ '}<span>{formatDay(item.valid_until)}</span></td>
                        <td className="p-2">{enabled.length > 0 ? enabled.join('、') : '无'}</td>
                        <td className="p-2">{item.max_concurrent_runs} / {item.max_user_concurrent_runs}</td>
                        <td className="p-2">
                          <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${item.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                            <span aria-hidden="true">{item.active ? '●' : '○'}</span>
                            {item.active ? '当前生效' : '历史版本'}
                          </span>
                        </td>
                        <td className="p-2">
                          {item.active ? (
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => setPendingStatus({ license: item, status: 'suspended' })}
                              className="rounded border border-rose-200 px-2 py-1 text-rose-600"
                            >
                              暂停
                            </button>
                          ) : (
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => setPendingStatus({ license: item, status: 'active' })}
                              className="rounded border border-slate-200 px-2 py-1"
                            >
                              激活
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <ConfirmDialog
        open={createOpen}
        title="追加 License 版本"
        description="新版本创建后为草稿状态，需激活后才参与授权判定。"
        confirmLabel="创建版本"
        busy={saving}
        confirmDisabled={!createValid}
        onConfirm={() => void submitCreate()}
        onCancel={() => setCreateOpen(false)}
      >
        <div className="mt-3 space-y-3">
          <fieldset>
            <legend className="mb-1 text-xs font-bold text-slate-600">功能开关</legend>
            <div className="grid grid-cols-2 gap-2">
              {FEATURE_KEYS.map(feature => (
                <label key={feature} htmlFor={`license-feature-${feature}`} className="flex items-center gap-2 text-xs text-slate-600">
                  <input
                    id={`license-feature-${feature}`}
                    type="checkbox"
                    checked={draftFeatures[feature] ?? false}
                    onChange={event => setDraftFeatures(value => ({ ...value, [feature]: event.target.checked }))}
                  />
                  {feature}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label htmlFor="license-max-runs" className="mb-1 block text-xs font-bold text-slate-600">租户并发上限</label>
              <input
                id="license-max-runs"
                type="number"
                min={1}
                max={1000}
                value={draftMaxRuns}
                onChange={event => setDraftMaxRuns(event.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
            <div>
              <label htmlFor="license-max-user-runs" className="mb-1 block text-xs font-bold text-slate-600">单用户并发上限</label>
              <input
                id="license-max-user-runs"
                type="number"
                min={1}
                max={1000}
                value={draftMaxUserRuns}
                onChange={event => setDraftMaxUserRuns(event.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
            <div>
              <label htmlFor="license-valid-from" className="mb-1 block text-xs font-bold text-slate-600">生效日期（可选）</label>
              <input
                id="license-valid-from"
                type="date"
                value={draftValidFrom}
                onChange={event => setDraftValidFrom(event.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
            <div>
              <label htmlFor="license-valid-until" className="mb-1 block text-xs font-bold text-slate-600">失效日期（可选）</label>
              <input
                id="license-valid-until"
                type="date"
                value={draftValidUntil}
                onChange={event => setDraftValidUntil(event.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
          </div>
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={pendingStatus !== null}
        title={pendingStatus?.status === 'active' ? '激活 License 版本' : '暂停 License 版本'}
        description={
          pendingStatus?.status === 'active'
            ? `确认激活 v${pendingStatus.license.version}？激活后它成为租户当前 License，只影响后续授权判定，进行中的 Run 不受影响。`
            : pendingStatus
              ? `确认暂停 v${pendingStatus.license.version}？暂停立即生效于后续授权判定，该租户的新 Run 将按无有效 License 处理。`
              : ''
        }
        confirmLabel={pendingStatus?.status === 'active' ? '确认激活' : '确认暂停'}
        busy={saving}
        onConfirm={() => void confirmStatus()}
        onCancel={() => setPendingStatus(null)}
      />
    </section>
  );
}
