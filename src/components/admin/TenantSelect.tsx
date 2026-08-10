import { useEffect, useId, useState } from 'react';

import { listAdminTenants, type AdminTenant } from '../../api/adminGateway';

type TenantSelectProps = {
  value: string;
  onChange: (tenantId: string) => void;
};

// 模块内共享的租户选择器：自行加载租户列表，未选择时返回空串。
export default function TenantSelect({ value, onChange }: TenantSelectProps) {
  const selectId = useId();
  const [tenants, setTenants] = useState<AdminTenant[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    void listAdminTenants({ limit: 200 })
      .then(result => setTenants(result.items))
      .catch(err => setError(err instanceof Error ? err.message : '租户列表加载失败'));
  }, []);

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={selectId} className="text-xs font-bold text-slate-600">选择租户</label>
      <select
        id={selectId}
        value={value}
        onChange={event => onChange(event.target.value)}
        className="rounded-lg border border-slate-200 px-3 py-2 text-xs"
      >
        <option value="">请选择租户</option>
        {tenants.map(tenant => (
          <option key={tenant.id} value={tenant.id}>{tenant.name}</option>
        ))}
      </select>
      {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
    </div>
  );
}
