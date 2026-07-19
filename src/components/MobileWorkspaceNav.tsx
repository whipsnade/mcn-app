import { BarChart3, MessageSquare, Sparkles } from 'lucide-react';


export type WorkspacePane = 'sessions' | 'chat' | 'bi';

interface MobileWorkspaceNavProps {
  active: WorkspacePane;
  onChange: (pane: WorkspacePane) => void;
}

const items = [
  { value: 'sessions' as const, label: '会话', icon: MessageSquare },
  { value: 'chat' as const, label: '分析对话', icon: Sparkles },
  { value: 'bi' as const, label: '分析报告', icon: BarChart3 },
];


export default function MobileWorkspaceNav({ active, onChange }: MobileWorkspaceNavProps) {
  return (
    <nav className="flex h-12 shrink-0 items-center gap-1 border-b border-slate-200 bg-white p-1.5 xl:hidden" aria-label="移动工作区导航">
      {items.map(item => {
        const Icon = item.icon;
        const isActive = active === item.value;
        return (
          <button
            key={item.value}
            type="button"
            aria-pressed={isActive}
            onClick={() => onChange(item.value)}
            className={`flex h-full flex-1 items-center justify-center gap-1.5 rounded-lg text-[11px] font-bold transition ${
              isActive
                ? 'bg-indigo-50 text-indigo-600 shadow-sm'
                : 'text-slate-400 hover:bg-slate-50 hover:text-slate-600'
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {item.label}
          </button>
        );
      })}
    </nav>
  );
}
