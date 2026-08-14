import { MessageSquare, Star } from 'lucide-react';

export type WorkspaceTab = 'chat' | 'favorites';

interface WorkspaceTabsProps {
  active: WorkspaceTab;
  onChange: (tab: WorkspaceTab) => void;
  favoriteCount: number;
}

// 顶部工作区只保留智能会话与收藏（design §13.2）：品牌/活动/达人属于右侧 BI，不作为快捷入口。
export function WorkspaceTabs({ active, onChange, favoriteCount }: WorkspaceTabsProps) {
  const tabs = [
    { id: 'chat' as const, label: '智能会话', icon: MessageSquare, title: '智能会话' },
    { id: 'favorites' as const, label: `已收藏 ${favoriteCount}`, icon: Star, title: '已收藏' },
  ];

  return (
    <div role="tablist" aria-label="会话工作区" className="flex h-11 shrink-0 overflow-x-auto border-b border-slate-200 bg-white px-4">
      {tabs.map(({ id, label, icon: Icon, title }) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={active === id}
          title={title}
          onClick={() => onChange(id)}
          className={active === id
            ? 'flex shrink-0 items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
            : 'flex shrink-0 items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
        >
          <Icon className="h-3.5 w-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}
