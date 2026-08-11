import { useCallback, useEffect, useId, useState } from 'react';

import {
  adjustTenantWallet,
  getTenantWallet,
  listAdminTenantUsers,
  listTenantQuota,
  setTenantQuota,
  type AdminQuotaItem,
  type AdminTenantUser,
} from '../../api/adminGateway';
import ConfirmDialog from './ConfirmDialog';

const QUOTA_MAX = 10_000_000;

// 钱包只读 404（无钱包行）的稳定错误码：退化文案，不当加载失败处理。
const WALLET_NOT_FOUND = 'tenant_wallet_not_found';

type PendingWallet = { userId: string; delta: number; reason: string };
type PendingQuota = { userId: string; pointsLimit: number };

const formatDelta = (delta: number): string => (delta > 0 ? `+${delta}` : String(delta));

// 租户成员的钱包调整与周期额度管理：挂在「用量与积分」模块下，按租户加载。
export default function TenantWalletQuota({ tenantId }: { tenantId: string }) {
  const baseId = useId();
  const [members, setMembers] = useState<AdminTenantUser[]>([]);
  const [quotaMap, setQuotaMap] = useState<Record<string, AdminQuotaItem>>({});
  const [quotaDraft, setQuotaDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 已知的钱包余额（来自只读端点或最近一次调整响应），用于确认文案展示「当前余额 → 调整后余额」。
  const [lastBalance, setLastBalance] = useState<number | null>(null);
  const [walletUserId, setWalletUserId] = useState('');
  const [deltaText, setDeltaText] = useState('');
  const [reasonText, setReasonText] = useState('');
  const [pendingWallet, setPendingWallet] = useState<PendingWallet | null>(null);
  const [pendingQuota, setPendingQuota] = useState<PendingQuota | null>(null);

  const reload = useCallback(async (id: string) => {
    setLoading(true);
    setError('');
    try {
      const [userResult, quotaResult, walletResult] = await Promise.all([
        listAdminTenantUsers(id),
        listTenantQuota(id),
        // 无钱包行（404 tenant_wallet_not_found）不算加载失败：确认文案退化为「以服务端返回为准」。
        getTenantWallet(id).catch((walletErr: unknown) => {
          if (walletErr instanceof Error && walletErr.message === WALLET_NOT_FOUND) return null;
          throw walletErr;
        }),
      ]);
      const map: Record<string, AdminQuotaItem> = {};
      const drafts: Record<string, string> = {};
      quotaResult.items.forEach(item => {
        map[item.user_id] = item;
        drafts[item.user_id] = String(item.points_limit);
      });
      setMembers(userResult.items);
      setQuotaMap(map);
      setQuotaDraft(drafts);
      setLastBalance(walletResult ? walletResult.balance : null);
    } catch (err) {
      setMembers([]);
      setQuotaMap({});
      setQuotaDraft({});
      setLastBalance(null);
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 切换租户时重置表单与已知余额，避免串租户展示旧数据。
    setLastBalance(null);
    setWalletUserId('');
    setDeltaText('');
    setReasonText('');
    setNotice('');
    setPendingWallet(null);
    setPendingQuota(null);
    void reload(tenantId);
  }, [tenantId, reload]);

  const parsedDelta = Number(deltaText);
  const walletValid =
    walletUserId !== '' &&
    deltaText.trim() !== '' &&
    Number.isInteger(parsedDelta) &&
    parsedDelta !== 0 &&
    reasonText.trim() !== '';

  const submitWallet = () => {
    if (!walletValid) return;
    setPendingWallet({ userId: walletUserId, delta: parsedDelta, reason: reasonText.trim() });
  };

  const confirmWallet = async () => {
    if (!pendingWallet) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const result = await adjustTenantWallet(tenantId, {
        user_id: pendingWallet.userId,
        delta: pendingWallet.delta,
        reason: pendingWallet.reason,
      });
      setLastBalance(result.balance);
      setNotice(`已调整，交易 ID：${result.transaction_id}，当前余额 ${result.balance} 积分`);
      setPendingWallet(null);
      setDeltaText('');
      setReasonText('');
      await reload(tenantId);
    } catch (err) {
      setError(err instanceof Error ? err.message : '钱包调整失败');
    } finally {
      setSaving(false);
    }
  };

  const quotaDraftValue = (userId: string): number | null => {
    const draft = (quotaDraft[userId] ?? '').trim();
    if (draft === '') return null;
    const value = Number(draft);
    if (!Number.isInteger(value) || value < 0 || value > QUOTA_MAX) return null;
    return value;
  };

  const submitQuota = (userId: string) => {
    const value = quotaDraftValue(userId);
    if (value === null || quotaMap[userId]?.points_limit === value) return;
    setPendingQuota({ userId, pointsLimit: value });
  };

  const confirmQuota = async () => {
    if (!pendingQuota) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      await setTenantQuota(tenantId, pendingQuota.userId, { points_limit: pendingQuota.pointsLimit });
      setPendingQuota(null);
      await reload(tenantId);
    } catch (err) {
      setError(err instanceof Error ? err.message : '额度保存失败');
    } finally {
      setSaving(false);
    }
  };

  const memberName = (userId: string): string =>
    members.find(member => member.id === userId)?.nickname ?? userId;

  const walletDescription = pendingWallet
    ? lastBalance === null
      ? `确认对成员 ${memberName(pendingWallet.userId)} 调整 ${formatDelta(pendingWallet.delta)} 积分？当前余额以服务端返回为准。原因：${pendingWallet.reason}`
      : `确认对成员 ${memberName(pendingWallet.userId)} 调整 ${formatDelta(pendingWallet.delta)} 积分？当前余额 ${lastBalance} → 调整后 ${lastBalance + pendingWallet.delta} 积分。原因：${pendingWallet.reason}`
    : '';

  const quotaDescription = pendingQuota
    ? `确认将成员 ${memberName(pendingQuota.userId)} 的周期额度上限从 ${quotaMap[pendingQuota.userId]?.points_limit ?? '未设置'} 调整为 ${pendingQuota.pointsLimit}？只影响新周期/新 Run 的扣费上限，在途 Run 与历史扣费不变。`
    : '';

  return (
    <section aria-labelledby="tenant-wallet-quota-title" className="space-y-4 border-t border-slate-100 pt-4">
      <h4 id="tenant-wallet-quota-title" className="text-sm font-bold">成员额度与钱包</h4>
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {notice && <p role="status" className="text-emerald-600">{notice}</p>}
      {!loading && !error && members.length === 0 && <p className="text-xs text-slate-400">暂无成员</p>}

      {members.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <caption className="sr-only">成员周期额度</caption>
              <thead>
                <tr className="text-slate-400">
                  <th className="p-2">成员</th>
                  <th className="p-2">角色</th>
                  <th className="p-2">周期额度上限</th>
                  <th className="p-2">额度状态</th>
                </tr>
              </thead>
              <tbody>
                {members.map(member => {
                  const quota = quotaMap[member.id];
                  const inputId = `${baseId}-quota-${member.id}`;
                  const draftValue = quotaDraftValue(member.id);
                  const unchanged = draftValue === null || quota?.points_limit === draftValue;
                  return (
                    <tr key={member.id} className="border-t border-slate-100">
                      <td className="p-2 font-bold">{member.nickname}</td>
                      <td className="p-2">{member.role}</td>
                      <td className="p-2">
                        <label htmlFor={inputId} className="sr-only">{member.nickname} 周期额度上限</label>
                        <input
                          id={inputId}
                          type="number"
                          min={0}
                          max={QUOTA_MAX}
                          value={quotaDraft[member.id] ?? ''}
                          placeholder="未设置"
                          disabled={saving}
                          onChange={event => setQuotaDraft(value => ({ ...value, [member.id]: event.target.value }))}
                          className="w-28 rounded-lg border border-slate-200 px-2 py-1"
                        />
                        <button
                          type="button"
                          className="ml-2 rounded border px-2 py-1 disabled:opacity-50"
                          disabled={saving || unchanged}
                          onClick={() => submitQuota(member.id)}
                        >
                          保存{member.nickname} 额度
                        </button>
                      </td>
                      <td className="p-2">{quota?.status ?? '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor={`${baseId}-wallet-user`} className="mb-1 block text-xs font-bold text-slate-600">调整成员</label>
              <select
                id={`${baseId}-wallet-user`}
                value={walletUserId}
                disabled={saving}
                onChange={event => setWalletUserId(event.target.value)}
                className="rounded-lg border border-slate-200 px-3 py-2 text-xs"
              >
                <option value="">请选择成员</option>
                {members.map(member => (
                  <option key={member.id} value={member.id}>{member.nickname}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor={`${baseId}-wallet-delta`} className="mb-1 block text-xs font-bold text-slate-600">调整积分</label>
              <input
                id={`${baseId}-wallet-delta`}
                type="number"
                step={1}
                value={deltaText}
                placeholder="正加负减"
                disabled={saving}
                onChange={event => setDeltaText(event.target.value)}
                className="w-28 rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
            <div className="min-w-40 flex-1">
              <label htmlFor={`${baseId}-wallet-reason`} className="mb-1 block text-xs font-bold text-slate-600">调整原因</label>
              <input
                id={`${baseId}-wallet-reason`}
                value={reasonText}
                maxLength={200}
                disabled={saving}
                onChange={event => setReasonText(event.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"
              />
            </div>
            <button
              type="button"
              disabled={saving || !walletValid}
              onClick={submitWallet}
              className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white disabled:opacity-50"
            >
              调整钱包
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={pendingWallet !== null}
        title="调整租户钱包"
        description={walletDescription}
        confirmLabel="确认调整"
        busy={saving}
        onConfirm={() => void confirmWallet()}
        onCancel={() => setPendingWallet(null)}
      />

      <ConfirmDialog
        open={pendingQuota !== null}
        title="保存周期额度"
        description={quotaDescription}
        confirmLabel="确认保存"
        busy={saving}
        onConfirm={() => void confirmQuota()}
        onCancel={() => setPendingQuota(null)}
      />
    </section>
  );
}
