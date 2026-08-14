import { useCallback, useEffect, useId, useState } from 'react';

import { listAdminUsage, type AdminUsage } from '../../api/adminGateway';
import TenantSelect from './TenantSelect';
import TenantWalletQuota from './TenantWalletQuota';

const PAGE_SIZE = 20;

type GroupBy = 'tenant' | 'user' | 'run' | 'day';

const GROUP_LABEL: Record<GroupBy, string> = {
  tenant: '租户',
  user: '用户',
  run: 'Run',
  day: '日期',
};

const groupKey = (item: AdminUsage): string => item.day ?? item.user_id ?? item.run_id ?? item.tenant_id;

const tokenText = (value: number | null): string => (value == null ? '—' : String(value));

export default function UsageAdmin({ tenantId: initialTenantId }: { tenantId?: string }) {
  const groupById = useId();
  const [tenantId, setTenantId] = useState(initialTenantId ?? '');
  const [groupBy, setGroupBy] = useState<GroupBy>('day');
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<AdminUsage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const reload = useCallback(async (id: string, group: GroupBy, pageOffset: number) => {
    setLoading(true);
    setError('');
    try {
      const result = await listAdminUsage(id, group, { limit: PAGE_SIZE, offset: pageOffset });
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
    void reload(tenantId, groupBy, offset);
  }, [tenantId, groupBy, offset, reload]);

  const hasNext = items.length === PAGE_SIZE;

  return (
    <section aria-labelledby="usage-admin-title" className="space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="usage-admin-title" className="text-sm font-bold">用量与积分</h3>
        <TenantSelect value={tenantId} onChange={next => { setTenantId(next); setOffset(0); }} />
      </div>
      {!tenantId && <p className="text-xs text-slate-400">请先选择租户</p>}
      {tenantId && (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={groupById} className="text-xs font-bold text-slate-600">分组维度</label>
          <select
            id={groupById}
            value={groupBy}
            onChange={event => { setGroupBy(event.target.value as GroupBy); setOffset(0); }}
            className="rounded-lg border border-slate-200 px-3 py-2 text-xs"
          >
            <option value="tenant">按租户</option>
            <option value="user">按用户</option>
            <option value="run">按 Run</option>
            <option value="day">按日期</option>
          </select>
        </div>
      )}
      {loading && <p role="status">加载中…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {tenantId && !loading && !error && items.length === 0 && <p className="text-xs text-slate-400">暂无用量记录</p>}
      {tenantId && <TenantWalletQuota tenantId={tenantId} />}
      {tenantId && items.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <caption className="sr-only">用量明细</caption>
              <thead>
                <tr className="text-slate-400">
                  <th className="p-2">{GROUP_LABEL[groupBy]}</th>
                  <th className="p-2">记录数</th>
                  <th className="p-2">输入 tokens</th>
                  <th className="p-2">输出 tokens</th>
                  <th className="p-2">缓存读/写</th>
                  <th className="p-2">成本 (micros)</th>
                  <th className="p-2">已定价成本</th>
                  <th className="p-2">异常标记</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => (
                  <tr key={`${groupKey(item)}-${index}`} className="border-t border-slate-100">
                    <td className="p-2 font-bold">{groupKey(item)}</td>
                    <td className="p-2">{item.record_count}</td>
                    <td className="p-2">{tokenText(item.input_tokens)}</td>
                    <td className="p-2">{tokenText(item.output_tokens)}</td>
                    <td className="p-2">{`${tokenText(item.cache_read_tokens)} / ${tokenText(item.cache_write_tokens)}`}</td>
                    <td className="p-2">{tokenText(item.cost_micros)}</td>
                    <td className="p-2">{item.priced_cost_micros}</td>
                    <td className="p-2">
                      {item.usage_unavailable_count === 0 && item.unpriced_count === 0 && <span className="text-slate-400">—</span>}
                      {item.usage_unavailable_count > 0 && (
                        <span className="mr-1 inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 font-bold text-amber-700">
                          <span aria-hidden="true">⚠</span>用量缺失 ×{item.usage_unavailable_count}
                        </span>
                      )}
                      {item.unpriced_count > 0 && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2 py-0.5 font-bold text-rose-600">
                          <span aria-hidden="true">⚠</span>未定价 ×{item.unpriced_count}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-end gap-2 text-xs">
            <span className="text-slate-400">第 {offset + 1} - {offset + items.length} 条</span>
            <button
              type="button"
              disabled={loading || offset === 0}
              onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}
              className="rounded border border-slate-200 px-2 py-1 disabled:opacity-50"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={loading || !hasNext}
              onClick={() => setOffset(value => value + PAGE_SIZE)}
              className="rounded border border-slate-200 px-2 py-1 disabled:opacity-50"
            >
              下一页
            </button>
          </div>
        </>
      )}
    </section>
  );
}
