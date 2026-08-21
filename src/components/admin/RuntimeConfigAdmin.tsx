import { useCallback, useEffect, useState } from 'react';

import {
  activateAdminRuntimeConfig,
  createAdminRuntimeConfig,
  listAdminRuntimeConfigs,
  type AdminRuntimeConfig,
} from '../../api/adminGateway';
import ConfirmDialog from './ConfirmDialog';
import TenantSelect from './TenantSelect';

type DraftForm = {
  runtime_backend: 'current' | 'pi';
  environment: 'development' | 'staging' | 'production';
  modelName: string;
  modelProvider: string;
  modelMaskedOrigin: string;
  datatapService: string;
  datatapSchemaDigest: string;
  maxDecisions: string;
  mcpCallPoints: string;
  secretBaseUrl: string;
  secretApiKey: string;
  secretDatatapToken: string;
  secretDatatapUrl: string;
};

const EMPTY_DRAFT: DraftForm = {
  runtime_backend: 'pi',
  environment: 'production',
  modelName: '',
  modelProvider: '',
  modelMaskedOrigin: '',
  datatapService: '',
  datatapSchemaDigest: '',
  maxDecisions: '50',
  mcpCallPoints: '10',
  secretBaseUrl: '',
  secretApiKey: '',
  secretDatatapToken: '',
  secretDatatapUrl: '',
};

const STATUS_LABEL: Record<AdminRuntimeConfig['status'], string> = {
  draft: '草稿',
  active: '生效中',
  retired: '已退役',
};

const formatTime = (value: string | null): string => (value ? value.slice(0, 10) : '—');

export default function RuntimeConfigAdmin({ tenantId: initialTenantId }: { tenantId?: string }) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? '');
  const [items, setItems] = useState<AdminRuntimeConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState<DraftForm>(EMPTY_DRAFT);
  const [pendingActivate, setPendingActivate] = useState<AdminRuntimeConfig | null>(null);

  const reload = useCallback(async (id: string) => {
    setLoading(true);
    setError('');
    try {
      const result = await listAdminRuntimeConfigs(id);
      setItems(result.items);
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

  const patchDraft = (patch: Partial<DraftForm>) => setDraft(value => ({ ...value, ...patch }));

  const secrets = [draft.secretBaseUrl, draft.secretApiKey, draft.secretDatatapToken, draft.secretDatatapUrl];
  const anySecret = secrets.some(value => value.trim() !== '');
  const allSecrets = secrets.every(value => value.trim() !== '');
  const baseValid =
    draft.modelName.trim() !== '' && draft.modelProvider.trim() !== '' && draft.modelMaskedOrigin.trim() !== '' &&
    draft.datatapService.trim() !== '' && draft.datatapSchemaDigest.trim() !== '' &&
    Number(draft.maxDecisions) > 0 && Number(draft.mcpCallPoints) > 0;
  const createValid = baseValid && (!anySecret || allSecrets);

  const submitCreate = async () => {
    if (!tenantId) return;
    setSaving(true);
    setError('');
    try {
      const payload: Record<string, unknown> = {
        tenant_id: tenantId,
        runtime_backend: draft.runtime_backend,
        environment: draft.environment,
        model: {
          name: draft.modelName.trim(),
          provider: draft.modelProvider.trim(),
          masked_origin: draft.modelMaskedOrigin.trim(),
        },
        datatap: { service: draft.datatapService.trim(), schema_digest: draft.datatapSchemaDigest.trim() },
        limits: { max_decisions: Number(draft.maxDecisions) },
        billing: { mcp_call_points: Number(draft.mcpCallPoints) },
      };
      if (anySecret) {
        payload.secrets = {
          model_base_url: draft.secretBaseUrl.trim(),
          model_api_key: draft.secretApiKey.trim(),
          datatap_token: draft.secretDatatapToken.trim(),
          datatap_urls: { mcp: draft.secretDatatapUrl.trim() },
        };
      }
      const created = await createAdminRuntimeConfig(payload);
      setItems(value => [...value, created]);
      setCreateOpen(false);
      // secret 为 write-only：提交后立即清空整个表单，页面只保留 masked/fingerprint 摘要。
      setDraft(EMPTY_DRAFT);
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败');
    } finally {
      setSaving(false);
    }
  };

  const confirmActivate = async () => {
    if (!pendingActivate || !tenantId) return;
    setSaving(true);
    setError('');
    try {
      await activateAdminRuntimeConfig(pendingActivate.id);
      setPendingActivate(null);
      await reload(tenantId);
    } catch (err) {
      setError(err instanceof Error ? err.message : '激活失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-labelledby="runtime-config-admin-title" className="space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="runtime-config-admin-title" className="text-sm font-bold">Runtime 配置</h3>
        <TenantSelect value={tenantId} onChange={setTenantId} />
      </div>
      <p className="text-xs text-slate-500">secret 为 write-only；页面不会读取或回显旧明文，仅展示 masked/fingerprint 摘要。</p>
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
              新建配置版本
            </button>
          </div>
          {items.length === 0 && <p className="text-xs text-slate-400">暂无 Runtime 配置版本</p>}
          {items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <caption className="sr-only">Runtime 配置版本历史</caption>
                <thead>
                  <tr className="text-slate-400">
                    <th className="p-2">版本</th>
                    <th className="p-2">状态</th>
                    <th className="p-2">Backend</th>
                    <th className="p-2">契约</th>
                    <th className="p-2">Secret 摘要</th>
                    <th className="p-2">创建时间</th>
                    <th className="p-2">激活时间</th>
                    <th className="p-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(item => (
                    <tr key={item.id} className="border-t border-slate-100">
                      <td className="p-2 font-bold">v{item.version}</td>
                      <td className="p-2">
                        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-bold ${
                          item.status === 'active' ? 'bg-emerald-50 text-emerald-700'
                            : item.status === 'draft' ? 'bg-amber-50 text-amber-700'
                              : 'bg-slate-100 text-slate-500'
                        }`}
                        >
                          <span aria-hidden="true">{item.status === 'active' ? '●' : item.status === 'draft' ? '◐' : '○'}</span>
                          {STATUS_LABEL[item.status]}
                        </span>
                      </td>
                      <td className="p-2">{item.runtime_backend} · {item.environment}</td>
                      <td className="p-2">{item.runtime_contract_version}</td>
                      <td className="p-2">
                        {item.secret_refs.length > 0
                          ? item.secret_refs.map((ref, index) => (
                            <span key={`${item.id}-secret-${index}`} className="mr-1 inline-block rounded bg-slate-50 px-1.5 py-0.5">
                              {`${ref.kind ?? 'secret'}: ${ref.fingerprint ?? 'stored'}`}
                            </span>
                          ))
                          : '无 secret'}
                      </td>
                      <td className="p-2">{formatTime(item.created_at)}</td>
                      <td className="p-2">{formatTime(item.activated_at)}</td>
                      <td className="p-2">
                        {item.status === 'draft' && (
                          <button
                            type="button"
                            disabled={saving}
                            onClick={() => setPendingActivate(item)}
                            className="rounded border border-slate-200 px-2 py-1"
                          >
                            激活
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <ConfirmDialog
        open={createOpen}
        title="新建 Runtime 配置版本"
        description="新版本以草稿创建，激活后才参与运行时解析；secret 提交后不可再读取。"
        confirmLabel="创建草稿"
        busy={saving}
        confirmDisabled={!createValid}
        onConfirm={() => void submitCreate()}
        onCancel={() => setCreateOpen(false)}
      >
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="rc-backend" className="mb-1 block text-xs font-bold text-slate-600">Runtime Backend</label>
            <select
              id="rc-backend"
              value={draft.runtime_backend}
              onChange={event => patchDraft({ runtime_backend: event.target.value as 'current' | 'pi' })}
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
            >
              <option value="pi">pi</option>
              <option value="current">current</option>
            </select>
          </div>
          <div>
            <label htmlFor="rc-environment" className="mb-1 block text-xs font-bold text-slate-600">运行环境</label>
            <select
              id="rc-environment"
              value={draft.environment}
              onChange={event => patchDraft({ environment: event.target.value as DraftForm['environment'] })}
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
            >
              <option value="development">development</option>
              <option value="staging">staging</option>
              <option value="production">production</option>
            </select>
          </div>
          <div>
            <label htmlFor="rc-model-name" className="mb-1 block text-xs font-bold text-slate-600">模型名称</label>
            <input id="rc-model-name" value={draft.modelName} onChange={event => patchDraft({ modelName: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-model-provider" className="mb-1 block text-xs font-bold text-slate-600">模型提供方</label>
            <input id="rc-model-provider" value={draft.modelProvider} onChange={event => patchDraft({ modelProvider: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-model-origin" className="mb-1 block text-xs font-bold text-slate-600">模型来源（脱敏）</label>
            <input id="rc-model-origin" value={draft.modelMaskedOrigin} onChange={event => patchDraft({ modelMaskedOrigin: event.target.value })} placeholder="https://api***" className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-datatap-service" className="mb-1 block text-xs font-bold text-slate-600">DataTap 服务名</label>
            <input id="rc-datatap-service" value={draft.datatapService} onChange={event => patchDraft({ datatapService: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-datatap-digest" className="mb-1 block text-xs font-bold text-slate-600">DataTap Schema 摘要</label>
            <input id="rc-datatap-digest" value={draft.datatapSchemaDigest} onChange={event => patchDraft({ datatapSchemaDigest: event.target.value })} placeholder="sha256:…" className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-max-decisions" className="mb-1 block text-xs font-bold text-slate-600">决策轮次上限</label>
            <input id="rc-max-decisions" type="number" min={1} value={draft.maxDecisions} onChange={event => patchDraft({ maxDecisions: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
          <div>
            <label htmlFor="rc-mcp-points" className="mb-1 block text-xs font-bold text-slate-600">MCP 调用积分</label>
            <input id="rc-mcp-points" type="number" min={1} value={draft.mcpCallPoints} onChange={event => patchDraft({ mcpCallPoints: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
          </div>
        </div>
        <fieldset className="mt-3">
          <legend className="mb-1 text-xs font-bold text-slate-600">Secrets（write-only，提交后不回显）</legend>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="rc-secret-base-url" className="mb-1 block text-xs text-slate-500">模型 Base URL</label>
              <input id="rc-secret-base-url" type="password" autoComplete="off" value={draft.secretBaseUrl} onChange={event => patchDraft({ secretBaseUrl: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
            </div>
            <div>
              <label htmlFor="rc-secret-api-key" className="mb-1 block text-xs text-slate-500">模型 API Key</label>
              <input id="rc-secret-api-key" type="password" autoComplete="off" value={draft.secretApiKey} onChange={event => patchDraft({ secretApiKey: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
            </div>
            <div>
              <label htmlFor="rc-secret-datatap-token" className="mb-1 block text-xs text-slate-500">DataTap Token</label>
              <input id="rc-secret-datatap-token" type="password" autoComplete="off" value={draft.secretDatatapToken} onChange={event => patchDraft({ secretDatatapToken: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
            </div>
            <div>
              <label htmlFor="rc-secret-datatap-url" className="mb-1 block text-xs text-slate-500">DataTap 服务 URL</label>
              <input id="rc-secret-datatap-url" type="password" autoComplete="off" value={draft.secretDatatapUrl} onChange={event => patchDraft({ secretDatatapUrl: event.target.value })} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
            </div>
          </div>
          {anySecret && !allSecrets && (
            <p role="alert" className="mt-2 text-xs text-amber-600">四个 secret 需全部填写，或全部留空</p>
          )}
        </fieldset>
      </ConfirmDialog>

      <ConfirmDialog
        open={pendingActivate !== null}
        title="激活 Runtime 配置版本"
        description={pendingActivate ? `确认激活 v${pendingActivate.version}？配置版本为 append-only：激活后当前生效版本自动转为 retired，新版本只影响后续 Run 的运行时解析。` : ''}
        confirmLabel="确认激活"
        busy={saving}
        onConfirm={() => void confirmActivate()}
        onCancel={() => setPendingActivate(null)}
      />
    </section>
  );
}
