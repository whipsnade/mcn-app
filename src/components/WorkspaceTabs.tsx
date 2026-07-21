import { FileSpreadsheet, Flame, MessageSquare, Star, Users, Zap } from 'lucide-react';

export type WorkspaceTab = 'chat' | 'favorites' | 'kol' | 'evaluate' | 'posts-xhs' | 'posts-dy';

export const QUICK_TAB_IDS: readonly WorkspaceTab[] = ['kol', 'evaluate', 'posts-xhs', 'posts-dy'];

interface WorkspaceTabsProps {
  active: WorkspaceTab;
  onChange: (tab: WorkspaceTab) => void;
  favoriteCount: number;
}

export function WorkspaceTabs({ active, onChange, favoriteCount }: WorkspaceTabsProps) {
  const tabs = [
    { id: 'chat' as const, label: '智能会话', icon: MessageSquare, title: '智能会话' },
    { id: 'favorites' as const, label: `已收藏 ${favoriteCount}`, icon: Star, title: '已收藏' },
    { id: 'kol' as const, label: '达人推荐', icon: Users, title: '预算内达人推荐（每次刷新约 20 积分）' },
    { id: 'evaluate' as const, label: '活动评估', icon: FileSpreadsheet, title: '达人/活动评估：上传 xlsx/csv 生成热度分析（免费）' },
    { id: 'posts-xhs' as const, label: '小红书爆贴', icon: Flame, title: '小红书前十爆贴（近 7 日，约 10~20 积分）' },
    { id: 'posts-dy' as const, label: '抖音爆贴', icon: Zap, title: '抖音前十爆贴（近 7 日，约 10~20 积分）' },
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
