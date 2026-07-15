import { MessageSquare, Star, Users } from 'lucide-react';

export type WorkspaceTab = 'chat' | 'candidates' | 'favorites';

interface WorkspaceTabsProps {
  active: WorkspaceTab;
  onChange: (tab: WorkspaceTab) => void;
  candidateCount: number;
  favoriteCount: number;
}

export function WorkspaceTabs({ active, onChange, candidateCount, favoriteCount }: WorkspaceTabsProps) {
  const tabs = [
    { id: 'chat' as const, label: '智能会话', icon: MessageSquare },
    { id: 'candidates' as const, label: `候选清单 ${candidateCount}`, icon: Users },
    { id: 'favorites' as const, label: `已收藏 ${favoriteCount}`, icon: Star },
  ];

  return (
    <div role="tablist" aria-label="会话工作区" className="flex h-11 shrink-0 border-b border-slate-200 bg-white px-4">
      {tabs.map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={active === id}
          onClick={() => onChange(id)}
          className={active === id
            ? 'flex items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
            : 'flex items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
        >
          <Icon className="h-3.5 w-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}
