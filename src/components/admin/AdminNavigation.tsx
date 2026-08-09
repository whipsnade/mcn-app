export type AdminModule = 'users' | 'tenants' | 'licenses' | 'usage' | 'pi-runtime' | 'runtime-configs' | 'diagnostics';

const MODULES: Array<{ id: AdminModule; label: string }> = [
  { id: 'users', label: '用户' }, { id: 'tenants', label: '租户' }, { id: 'licenses', label: 'License' },
  { id: 'usage', label: '用量与积分' }, { id: 'pi-runtime', label: 'Pi Runtime' },
  { id: 'runtime-configs', label: 'Runtime 配置' }, { id: 'diagnostics', label: 'Run 诊断' },
];

export function AdminNavigation({ active, onChange }: { active: AdminModule; onChange: (module: AdminModule) => void }) {
  return (
    <nav aria-label="管理员模块" className="flex gap-1 overflow-x-auto border-b border-slate-100 bg-white px-4 py-2 md:w-44 md:flex-col md:overflow-visible md:border-b-0 md:border-r md:px-3">
      {MODULES.map(item => (
        <button key={item.id} type="button" aria-current={active === item.id ? 'page' : undefined}
          onClick={() => onChange(item.id)}
          className={`shrink-0 rounded-lg px-3 py-2 text-left text-xs font-bold transition ${active === item.id ? 'bg-indigo-50 text-indigo-700' : 'text-slate-500 hover:bg-slate-50'}`}>
          {item.label}
        </button>
      ))}
    </nav>
  );
}

export default AdminNavigation;
