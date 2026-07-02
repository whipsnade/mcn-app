import React, { useState } from 'react';
import { Plus, Search, MessageSquare, Layers, Sparkles, SlidersHorizontal, BarChart3 } from 'lucide-react';
import { Session } from '../types';

interface SessionListProps {
  sessions: Session[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onOpenNewModal: () => void;
}

export default function SessionList({
  sessions,
  activeSessionId,
  onSelectSession,
  onOpenNewModal
}: SessionListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [points, setPoints] = useState(() => {
    const saved = localStorage.getItem('kol_analyst_points');
    return saved ? parseInt(saved, 10) : 3450;
  });
  const [showRechargeToast, setShowRechargeToast] = useState(false);
  const maxPoints = 5000;

  // Filter sessions by Brand, Campaign, MCN, or message content
  const filteredSessions = sessions.filter(s => {
    const query = searchQuery.toLowerCase();
    const matchMeta = 
      s.brand.toLowerCase().includes(query) ||
      s.campaignName.toLowerCase().includes(query) ||
      s.title.toLowerCase().includes(query) ||
      s.mcn.toLowerCase().includes(query);
    
    const matchMessages = s.messages.some(m => m.text.toLowerCase().includes(query));
    
    return matchMeta || matchMessages;
  });

  return (
    <div className="flex h-full flex-col bg-white border-r border-slate-200 w-80 shrink-0 no-print">
      
      {/* List Header */}
      <div className="p-4 border-b border-slate-100 bg-white">
        <div className="flex items-center justify-between mb-3.5">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">A</div>
            <h1 className="font-bold text-slate-800 text-sm tracking-tight font-display">KOL Insight AI</h1>
          </div>
          
          <div className="flex items-center gap-1">
            <button 
              onClick={onOpenNewModal}
              className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm transition active:scale-95"
              title="新建分析会话"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
            <button 
              className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition"
              title="大盘层级"
            >
              <Layers className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="relative mt-2">
          <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-slate-400" />
          <input
            type="text"
            placeholder="Search sessions..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            className="w-full bg-slate-100 border-none rounded-md py-1.5 pl-8.5 pr-3 text-xs focus:ring-2 focus:ring-indigo-500 text-slate-700 placeholder-slate-400 transition outline-none"
          />
        </div>
      </div>

      {/* Sessions Cards Container */}
      <div className="flex-1 overflow-y-auto p-2.5 space-y-1.5">
        {filteredSessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
            <Search className="h-8 w-8 text-slate-300 mb-2" />
            <p className="text-xs font-medium text-slate-400">未找到匹配的分析会话</p>
          </div>
        ) : (
          filteredSessions.map(session => {
            const isActive = session.id === activeSessionId;
            const lastMessage = session.messages[session.messages.length - 1];
            const unreadCount = session.id === "WO-1001" ? 2 : 0; 

            // Get platform logo colors and initials
            const getPlatformStyle = (pf: string) => {
              switch (pf) {
                case 'Xiaohongshu': return { bg: 'bg-rose-50 text-rose-600 border border-rose-200/40', label: '小红书' };
                case 'Douyin': return { bg: 'bg-slate-900 text-white', label: '抖音' };
                case 'Bilibili': return { bg: 'bg-pink-50 text-pink-500 border border-pink-200/40', label: 'B站' };
                case 'Weibo': return { bg: 'bg-amber-50 text-amber-600 border border-amber-200/40', label: '微博' };
                case 'YouTube': return { bg: 'bg-red-50 text-red-600 border border-red-200/40', label: 'YouTube' };
                case 'Instagram': return { bg: 'bg-indigo-50 text-indigo-700 border border-indigo-200/40', label: 'Instagram' };
                default: return { bg: 'bg-indigo-50 text-indigo-700 border border-indigo-100', label: pf || '推广' };
              }
            };
            const brandInitial = session.brand.charAt(0);

            return (
              <div
                key={session.id}
                onClick={() => onSelectSession(session.id)}
                className={`p-3 rounded-lg flex flex-col gap-1 cursor-pointer transition border duration-150 ${
                  isActive 
                    ? 'bg-indigo-50 text-indigo-700 border-indigo-100/50' 
                    : 'hover:bg-slate-50 text-slate-600 border-transparent'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className={`font-semibold text-xs ${isActive ? 'text-indigo-800' : 'text-slate-800'}`}>
                    {session.brand.split(' ')[0]} - {session.campaignName}
                  </span>
                  <span className="text-[10px] opacity-70">
                    {session.id === "WO-1001" ? "2分钟前" : session.id === "WO-1002" ? "4小时前" : "1天前"}
                  </span>
                </div>

                <div className="flex items-center justify-between text-[11px] mt-1">
                  <p className="opacity-80 truncate max-w-[150px]">
                    {lastMessage ? lastMessage.text : session.summary}
                  </p>
                  <div className="flex flex-wrap gap-1 justify-end shrink-0 max-w-[100px]">
                    {session.platform.split(',').map((p) => {
                      const trimmed = p.trim();
                      const style = getPlatformStyle(trimmed);
                      return (
                        <span key={trimmed} className={`text-[9px] font-semibold px-1 py-0.2 rounded scale-90 ${style.bg}`}>
                          {style.label}
                        </span>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Profile/Workspace Status Bar at bottom */}
      <div className="p-4 border-t border-slate-100 bg-white shrink-0 space-y-2.5 relative">
        <div className="flex items-center justify-between">
          <div className="flex flex-col">
            <span className="text-[10px] font-bold text-slate-400">积分额度</span>
            <span className="text-xs font-bold text-slate-700 font-display">
              {points.toLocaleString()} / {maxPoints.toLocaleString()} 点
            </span>
          </div>
          <button
            onClick={() => {
              const newPoints = Math.min(points + 1000, maxPoints);
              setPoints(newPoints);
              localStorage.setItem('kol_analyst_points', newPoints.toString());
              setShowRechargeToast(true);
              setTimeout(() => setShowRechargeToast(false), 2000);
            }}
            className="px-2.5 py-1 text-[10px] font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md transition duration-150 active:scale-95 shadow-sm"
          >
            充值
          </button>
        </div>

        <div className="space-y-1">
          <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
            <div 
              className="h-full bg-indigo-600 rounded-full transition-all duration-500 ease-out" 
              style={{ width: `${(points / maxPoints) * 100}%` }}
            />
          </div>
          <div className="flex justify-between text-[9px] font-medium text-slate-400">
            <span>已用 {(((maxPoints - points) / maxPoints) * 100).toFixed(0)}%</span>
            <span>PRO PLAN 额度充足</span>
          </div>
        </div>

        {/* Toast Notification */}
        {showRechargeToast && (
          <div className="absolute -top-12 left-4 right-4 bg-emerald-600 text-white text-[10px] font-bold px-3 py-2 rounded-lg shadow-md flex items-center justify-center gap-1.5 animate-bounce z-10">
            <span className="h-1.5 w-1.5 rounded-full bg-white animate-ping" />
            充值成功！已补充 1,000 点额度
          </div>
        )}
      </div>

    </div>
  );
}
