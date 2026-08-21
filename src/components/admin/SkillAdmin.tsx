import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type {
  AdminSkillEnvironment,
  ApiSkillDetail,
  ApiSkillRevision,
  ApiSkillValidation,
} from '../../api/contracts';
import {
  activateAdminSkill,
  createAdminSkillRevision,
  getAdminSkill,
  getAdminSkillDiff,
  listAdminSkills,
  rollbackAdminSkill,
  validateAdminSkill,
} from '../../api/skills';
import ConfirmDialog from './ConfirmDialog';

const ENVIRONMENT_LABEL: Record<AdminSkillEnvironment, string> = {
  development: '开发',
  staging: '预发布',
  production: '生产',
};

const errorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

const formatDate = (value: string | null): string => (value ? value.slice(0, 10) : '—');

export default function SkillAdmin() {
  const [items, setItems] = useState<Awaited<ReturnType<typeof listAdminSkills>>['items']>([]);
  const [selectedSkillName, setSelectedSkillName] = useState('');
  const [detail, setDetail] = useState<ApiSkillDetail | null>(null);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState('');
  const [changeNote, setChangeNote] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [environment, setEnvironment] = useState<AdminSkillEnvironment>('production');
  const [rolloutPercent, setRolloutPercent] = useState('100');
  const [fromRevision, setFromRevision] = useState<number | null>(null);
  const [toRevision, setToRevision] = useState<number | null>(null);
  const [fromRevisionId, setFromRevisionId] = useState<string | null>(null);
  const [toRevisionId, setToRevisionId] = useState<string | null>(null);
  const [validation, setValidation] = useState<ApiSkillValidation | null>(null);
  const [diff, setDiff] = useState('');
  const [pendingRollback, setPendingRollback] = useState(false);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const operationKeys = useRef(new Map<string, string>());

  const operationKey = (operation: string, fingerprint: unknown): string => {
    const identity = `${operation}:${JSON.stringify(fingerprint)}`;
    const existing = operationKeys.current.get(identity);
    if (existing) return existing;
    const created = crypto.randomUUID();
    operationKeys.current.set(identity, created);
    return created;
  };

  const completeOperation = (operation: string, fingerprint: unknown) => {
    operationKeys.current.delete(`${operation}:${JSON.stringify(fingerprint)}`);
  };

  const loadDetail = useCallback(async (skillName: string) => {
    setDetailLoading(true);
    setError('');
    try {
      const next = await getAdminSkill(skillName);
      setDetail(next);
      const latest = next.revisions[0] ?? null;
      setSelectedRevision(latest?.revision ?? null);
      setSelectedRevisionId(latest?.id ?? null);
      setDraftContent(latest?.content ?? '');
      setChangeNote('');
      setValidation(null);
      setDiff('');
      setFromRevision(next.revisions[1]?.revision ?? latest?.revision ?? null);
      setToRevision(latest?.revision ?? null);
      setFromRevisionId(next.revisions[1]?.id ?? latest?.id ?? null);
      setToRevisionId(latest?.id ?? null);
    } catch (loadError) {
      setError(errorMessage(loadError, '加载 Skill 详情失败'));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const loadList = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await listAdminSkills();
      setItems(next.items);
      setSelectedSkillName(current => current || next.items[0]?.skill_name || '');
    } catch (loadError) {
      setError(errorMessage(loadError, '加载 Skill 列表失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    if (selectedSkillName) void loadDetail(selectedSkillName);
  }, [loadDetail, selectedSkillName]);

  const currentRevision = useMemo<ApiSkillRevision | null>(() => {
    if (!detail || (selectedRevisionId === null && selectedRevision === null)) return null;
    return detail.revisions.find(revision => revision.id === selectedRevisionId)
      ?? detail.revisions.find(revision => revision.revision === selectedRevision)
      ?? null;
  }, [detail, selectedRevision, selectedRevisionId]);

  const refreshAfterMutation = async () => {
    if (selectedSkillName) await loadDetail(selectedSkillName);
    await loadList();
  };

  const handleSelectRevision = (revision: ApiSkillRevision) => {
    setSelectedRevision(revision.revision);
    setSelectedRevisionId(revision.id);
    setDraftContent(revision.content);
    setChangeNote('');
    setValidation(null);
  };

  const handleValidate = async () => {
    if (!selectedSkillName) return;
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      const result = await validateAdminSkill({
        expected_name: selectedSkillName,
        content: draftContent,
      });
      setValidation(result);
      if (result.valid) setSuccess('Skill 校验通过');
    } catch (validateError) {
      setError(errorMessage(validateError, 'Skill 校验失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleCreateRevision = async () => {
    if (!selectedSkillName || !draftContent.trim()) return;
    const fingerprint = {
      content: draftContent,
      tenant_id: tenantId.trim() || null,
      change_note: changeNote.trim() || null,
    };
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await createAdminSkillRevision(
        selectedSkillName,
        fingerprint,
        operationKey('revision-create', { skill: selectedSkillName, ...fingerprint }),
      );
      completeOperation('revision-create', { skill: selectedSkillName, ...fingerprint });
      setSuccess('新 Revision 已保存');
      await refreshAfterMutation();
    } catch (createError) {
      setError(errorMessage(createError, '保存 Revision 失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleDiff = async () => {
    if (!selectedSkillName || fromRevision === null || toRevision === null) return;
    setBusy(true);
    setError('');
    try {
      const scopeTenantId = tenantId.trim() || undefined;
      const result = await getAdminSkillDiff(
        selectedSkillName,
        fromRevision,
        toRevision,
        scopeTenantId,
        fromRevisionId ?? undefined,
        toRevisionId ?? undefined,
      );
      setDiff(result.diff);
    } catch (diffError) {
      setError(errorMessage(diffError, '加载 Diff 失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleActivate = async (percent: number = Number(rolloutPercent)) => {
    if (!selectedSkillName || selectedRevision === null) return;
    const normalizedPercent = Number.isFinite(percent) ? Math.min(100, Math.max(0, Math.trunc(percent))) : 100;
    const fingerprint = {
      revision_id: currentRevision?.id,
      revision: selectedRevision,
      tenant_id: tenantId.trim() || null,
      environment,
      rollout_percent: normalizedPercent,
    };
    setRolloutPercent(String(normalizedPercent));
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await activateAdminSkill(
        selectedSkillName,
        {
          revision: selectedRevision,
          revision_id: currentRevision?.id,
          tenant_id: tenantId.trim() || null,
          environment,
          rollout_percent: normalizedPercent,
        },
        operationKey('skill-activate', { skill: selectedSkillName, ...fingerprint }),
      );
      completeOperation('skill-activate', { skill: selectedSkillName, ...fingerprint });
      setSuccess(normalizedPercent === 100 ? 'Skill 已全量激活' : 'Skill 灰度激活成功');
      await refreshAfterMutation();
    } catch (activateError) {
      setError(errorMessage(activateError, '激活 Skill 失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleRollback = async () => {
    if (!selectedSkillName) return;
    const fingerprint = { tenant_id: tenantId.trim() || null, environment };
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await rollbackAdminSkill(
        selectedSkillName,
        fingerprint,
        operationKey('skill-rollback', { skill: selectedSkillName, ...fingerprint }),
      );
      completeOperation('skill-rollback', { skill: selectedSkillName, ...fingerprint });
      setPendingRollback(false);
      setSuccess('Skill 已回滚到上一 Revision');
      await refreshAfterMutation();
    } catch (rollbackError) {
      setError(errorMessage(rollbackError, '回滚 Skill 失败'));
    } finally {
      setBusy(false);
    }
  };

  const selectedActivation = detail?.activations.find(
    activation => activation.environment === environment
      && activation.tenant_id === (tenantId.trim() || null),
  );

  return (
    <section aria-labelledby="skill-admin-title" className="space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 id="skill-admin-title" className="text-sm font-bold">营销 Skills</h3>
          <p className="mt-1 text-xs text-slate-500">Revision 不可变；激活只影响后续 Run，运行中 Run 使用既有快照。</p>
        </div>
        <span className="rounded-full bg-indigo-50 px-2 py-1 text-[11px] font-bold text-indigo-700">数据库事实源</span>
      </div>

      {error && <p role="alert" className="rounded-lg bg-rose-50 p-2 text-xs text-rose-700">{error}</p>}
      {success && <p role="status" className="rounded-lg bg-emerald-50 p-2 text-xs text-emerald-700">{success}</p>}
      {loading && <p role="status">加载中…</p>}

      {!loading && (
        <div className="grid min-h-[520px] gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
          <aside className="rounded-xl border border-slate-100 bg-slate-50/60 p-2">
            <div className="px-2 py-2 text-[11px] font-bold text-slate-400">Skill 列表（{items.length}）</div>
            {items.length === 0 && <p className="px-2 py-4 text-xs text-slate-400">暂无 Skill</p>}
            <div className="space-y-1">
              {items.map(item => (
                <button
                  key={item.skill_name}
                  type="button"
                  aria-label={`选择 Skill ${item.skill_name}`}
                  aria-current={selectedSkillName === item.skill_name ? 'page' : undefined}
                  onClick={() => setSelectedSkillName(item.skill_name)}
                  className={`w-full rounded-lg px-3 py-2 text-left text-xs transition ${selectedSkillName === item.skill_name ? 'bg-white font-bold text-indigo-700 shadow-sm' : 'text-slate-600 hover:bg-white'}`}
                >
                  <span className="block truncate">{item.skill_name}</span>
                  <span className="mt-1 block text-[10px] text-slate-400">最新 v{item.latest_revision} · {item.revision_count} 个 Revision</span>
                </button>
              ))}
            </div>
          </aside>

          <div className="min-w-0 space-y-4">
            {detailLoading && <p role="status">加载 Skill 详情…</p>}
            {!detailLoading && detail && currentRevision && (
              <>
                <div className="rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h4 className="text-sm font-bold text-slate-800">{detail.skill_name}</h4>
                      <p className="mt-1 text-xs text-slate-500">{currentRevision.description || '无描述'}</p>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {detail.activations.map(activation => (
                        <span key={activation.id} className="rounded-full bg-emerald-50 px-2 py-1 text-[10px] font-bold text-emerald-700">
                          {ENVIRONMENT_LABEL[activation.environment]} / {activation.scope_key} / {activation.rollout_percent === 100 ? '全量' : `${activation.rollout_percent}%`} · v{activation.active_revision}
                        </span>
                      ))}
                    </div>
                  </div>
                  {selectedActivation && (
                    <p className="mt-2 text-[10px] text-slate-400">
                      更新人：{selectedActivation.updated_by ?? '—'} · {formatDate(selectedActivation.updated_at)}
                    </p>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2" aria-label="Revision 列表">
                    {detail.revisions.map(revision => (
                      <button
                        key={revision.id}
                        type="button"
                        aria-label={`选择 Revision ${revision.revision}`}
                        onClick={() => handleSelectRevision(revision)}
                        className={`rounded-lg border px-3 py-2 text-left text-[11px] ${selectedRevisionId === revision.id ? 'border-indigo-200 bg-indigo-50 text-indigo-700' : 'border-slate-200 text-slate-600'}`}
                      >
                        <span className="block font-bold">Revision v{revision.revision}</span>
                        <span className="block text-[10px] text-slate-400">创建人：{revision.created_by ?? '—'}</span>
                        <span className="block text-[10px] text-slate-400">{formatDate(revision.created_at)} · {revision.change_note ?? '无变更说明'}</span>
                      </button>
                    ))}
                  </div>
                </div>

                <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
                  <div className="space-y-3 rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label htmlFor="skill-markdown" className="text-xs font-bold text-slate-700">编辑 Markdown</label>
                      <span className="text-[10px] text-slate-400">{currentRevision.content_digest}</span>
                    </div>
                    <textarea
                      id="skill-markdown"
                      aria-label="Skill Markdown"
                      value={draftContent}
                      onChange={event => setDraftContent(event.target.value)}
                      className="min-h-[280px] w-full rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-700 outline-none focus:border-indigo-400 focus:bg-white"
                    />
                    <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                      <div>
                        <label htmlFor="skill-change-note" className="mb-1 block text-[11px] font-bold text-slate-600">变更说明</label>
                        <input
                          id="skill-change-note"
                          aria-label="变更说明"
                          value={changeNote}
                          onChange={event => setChangeNote(event.target.value)}
                          placeholder="说明本次 Revision 的目的"
                          className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
                        />
                      </div>
                      <div className="flex items-end gap-2">
                        <button type="button" disabled={busy || !draftContent.trim()} onClick={() => void handleValidate()} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-700 disabled:opacity-50">校验当前内容</button>
                        <button type="button" disabled={busy || !draftContent.trim()} onClick={() => void handleCreateRevision()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white disabled:opacity-50">保存新 Revision</button>
                      </div>
                    </div>
                    {validation && (
                      <div className={`rounded-lg p-3 text-xs ${validation.valid ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'}`}>
                        <p className="font-bold">{validation.valid ? '校验通过' : '校验未通过'} · {validation.content_digest}</p>
                        {!validation.valid && (
                          <ul className="mt-2 list-disc space-y-1 pl-4">
                            {validation.errors.map((item, index) => <li key={`${item.code}-${index}`}>{item.code}：{item.message}{item.line ? `（第 ${item.line} 行）` : ''}</li>)}
                          </ul>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="space-y-4">
                    <div className="space-y-3 rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                      <h4 className="text-xs font-bold text-slate-700">激活与灰度</h4>
                      <div>
                        <label htmlFor="skill-tenant-id" className="mb-1 block text-[11px] font-bold text-slate-600">租户 ID</label>
                        <input id="skill-tenant-id" aria-label="租户 ID" value={tenantId} onChange={event => setTenantId(event.target.value)} placeholder="留空表示全局" className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs" />
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <label htmlFor="skill-environment" className="mb-1 block text-[11px] font-bold text-slate-600">环境</label>
                          <select id="skill-environment" value={environment} onChange={event => setEnvironment(event.target.value as AdminSkillEnvironment)} className="w-full rounded-lg border border-slate-200 px-2 py-2 text-xs">
                            <option value="development">开发</option>
                            <option value="staging">预发布</option>
                            <option value="production">生产</option>
                          </select>
                        </div>
                        <div>
                          <label htmlFor="skill-rollout-percent" className="mb-1 block text-[11px] font-bold text-slate-600">灰度比例</label>
                          <input id="skill-rollout-percent" aria-label="灰度比例" type="number" min="0" max="100" value={rolloutPercent} onChange={event => setRolloutPercent(event.target.value)} className="w-full rounded-lg border border-slate-200 px-2 py-2 text-xs" />
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button type="button" disabled={busy} onClick={() => void handleActivate()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white disabled:opacity-50">激活当前 Revision</button>
                        <button type="button" disabled={busy} onClick={() => void handleActivate(100)} className="rounded-lg border border-indigo-200 px-3 py-2 text-xs font-bold text-indigo-700 disabled:opacity-50">全量激活</button>
                      </div>
                      {selectedActivation?.previous_revision_id && (
                        <button type="button" disabled={busy} onClick={() => setPendingRollback(true)} className="text-xs font-bold text-rose-600 underline disabled:opacity-50">回滚上一 Revision</button>
                      )}
                    </div>

                    <div className="space-y-3 rounded-xl border border-slate-100 bg-white p-4 shadow-sm">
                      <div className="flex items-center justify-between gap-2">
                        <h4 className="text-xs font-bold text-slate-700">数据库 Diff</h4>
                        <button type="button" disabled={busy || fromRevision === null || toRevision === null} onClick={() => void handleDiff()} className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-[11px] font-bold text-slate-700 disabled:opacity-50">加载 Diff</button>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <label className="text-[10px] text-slate-500">From
                          <select aria-label="From Revision" value={fromRevisionId ?? ''} onChange={event => { const revision = detail.revisions.find(item => item.id === event.target.value); setFromRevisionId(event.target.value); setFromRevision(revision?.revision ?? null); }} className="mt-1 w-full rounded border border-slate-200 px-2 py-1.5 text-xs">
                            {detail.revisions.map(revision => <option key={revision.id} value={revision.id}>v{revision.revision} · {revision.scope_key}</option>)}
                          </select>
                        </label>
                        <label className="text-[10px] text-slate-500">To
                          <select aria-label="To Revision" value={toRevisionId ?? ''} onChange={event => { const revision = detail.revisions.find(item => item.id === event.target.value); setToRevisionId(event.target.value); setToRevision(revision?.revision ?? null); }} className="mt-1 w-full rounded border border-slate-200 px-2 py-1.5 text-xs">
                            {detail.revisions.map(revision => <option key={revision.id} value={revision.id}>v{revision.revision} · {revision.scope_key}</option>)}
                          </select>
                        </label>
                      </div>
                      {diff && <pre className="max-h-48 overflow-auto rounded-lg bg-slate-950 p-3 text-[10px] leading-relaxed text-slate-100">{diff}</pre>}
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={pendingRollback}
        title="回滚 Skill 激活"
        description="回滚会把当前环境与租户指针切回上一 Revision，并保留本次回滚审计记录；已有 Run 的 snapshot 不会改变。"
        confirmLabel="确认回滚"
        busy={busy}
        onConfirm={() => void handleRollback()}
        onCancel={() => setPendingRollback(false)}
      />
    </section>
  );
}
