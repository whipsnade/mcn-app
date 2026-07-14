import React, { useState } from 'react';
import { Plus, Search, MessageSquare, Layers, Sparkles, SlidersHorizontal, BarChart3, LogOut, Star, Edit2, Check, X, Shield } from 'lucide-react';
import { Session } from '../types';

interface SessionListProps {
  sessions: Session[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onOpenNewModal: () => void;
  onToggleStar?: (id: string) => void;
  onRenameSession?: (id: string, brand: string, campaignName: string) => void;
  user?: { phone?: string; loginMethod: 'sms' | 'wechat'; nickname: string } | null;
  onLogout?: () => void;
  points: number;
  onOpenRecharge: () => void;
  onOpenAdmin: () => void;
}

export default function SessionList({
  sessions,
  activeSessionId,
  onSelectSession,
  onOpenNewModal,
  onToggleStar,
  onRenameSession,
  user,
  onLogout,
  points,
  onOpenRecharge,
  onOpenAdmin
 }: SessionListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [showOnlyStarred, setShowOnlyStarred] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editBrand, setEditBrand] = useState('');
  const [editCampaignName, setEditCampaignName] = useState('');

  const handleStartEdit = (session: Session) => {
    setEditingSessionId(session.id);
    setEditBrand(session.brand);
    setEditCampaignName(session.campaignName);
  };

  const handleSaveEdit = (id: string) => {
    if (onRenameSession && editBrand.trim() && editCampaignName.trim()) {
      onRenameSession(id, editBrand.trim(), editCampaignName.trim());
    }
    setEditingSessionId(null);
  };

  const handleCancelEdit = () => {
    setEditingSessionId(null);
  };
  const [showRechargeToast, setShowRechargeToast] = useState(false);
  const maxPoints = 5000;

  // Filter sessions by Brand, Campaign, MCN, or message content, and optionally Starred status
  const filteredSessions = sessions.filter(s => {
    if (showOnlyStarred && !s.isStarred) {
      return false;
    }

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
              onClick={onOpenAdmin}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-indigo-600 transition"
              title="管理员控制台"
            >
              <Shield className="h-3.5 w-3.5" />
            </button>
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

        {/* Search Bar & Starred filter */}
        <div className="flex items-center gap-1.5 mt-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-slate-400" />
            <input
              type="text"
              placeholder="搜索会话、品牌..."
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              className="w-full bg-slate-100 border-none rounded-md py-1.5 pl-8.5 pr-3 text-xs focus:ring-2 focus:ring-indigo-500 text-slate-700 placeholder-slate-400 transition outline-none"
            />
          </div>
          <button
            onClick={() => setShowOnlyStarred(!showOnlyStarred)}
            className={`flex h-7 px-2 items-center gap-1 justify-center rounded-lg transition active:scale-95 border shrink-0 text-[11px] font-bold ${
              showOnlyStarred 
                ? 'bg-amber-50 text-amber-600 border-amber-200/60' 
                : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
            }`}
            title={showOnlyStarred ? "查看全部项目" : "仅看已标星重点项目"}
          >
            <Star className={`h-3 w-3 ${showOnlyStarred ? 'fill-amber-400 text-amber-500' : ''}`} />
            <span>重点</span>
          </button>
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
                {session.id === editingSessionId ? (
                  <div 
                    onClick={(e) => e.stopPropagation()} 
                    className="flex flex-col gap-1.5 w-full bg-slate-50 p-2 rounded-lg border border-indigo-100"
                  >
                    <div className="flex gap-1 items-center">
                      <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider shrink-0 w-8">品牌:</span>
                      <input
                        type="text"
                        value={editBrand}
                        onChange={e => setEditBrand(e.target.value)}
                        className="flex-1 min-w-0 bg-white border border-slate-200 rounded px-1.5 py-0.5 text-xs font-semibold text-slate-800 outline-none focus:border-indigo-500 transition"
                        placeholder="例如: 完美日记"
                        autoFocus
                      />
                    </div>
                    <div className="flex gap-1 items-center">
                      <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider shrink-0 w-8">活动:</span>
                      <input
                        type="text"
                        value={editCampaignName}
                        onChange={e => setEditCampaignName(e.target.value)}
                        className="flex-1 min-w-0 bg-white border border-slate-200 rounded px-1.5 py-0.5 text-xs text-slate-700 outline-none focus:border-indigo-500 transition"
                        placeholder="例如: 新品宣发"
                      />
                    </div>
                    <div className="flex justify-end gap-1.5 pt-1 border-t border-slate-100">
                      <button
                        onClick={handleCancelEdit}
                        className="px-2 py-0.5 text-[10px] font-bold text-slate-500 bg-white hover:bg-slate-100 border border-slate-200 rounded-md transition active:scale-95 flex items-center gap-0.5"
                        title="取消"
                      >
                        <X className="h-2.5 w-2.5" />
                        <span>取消</span>
                      </button>
                      <button
                        onClick={() => handleSaveEdit(session.id)}
                        className="px-2 py-0.5 text-[10px] font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md transition active:scale-95 flex items-center gap-0.5"
                        title="保存"
                      >
                        <Check className="h-2.5 w-2.5" />
                        <span>保存</span>
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between w-full">
                    <span className={`font-semibold text-xs truncate max-w-[140px] ${isActive ? 'text-indigo-800' : 'text-slate-800'}`}>
                      {session.brand.split(' ')[0]} - {session.campaignName}
                    </span>
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleStartEdit(session);
                        }}
                        className="p-1 rounded text-slate-300 hover:text-indigo-600 hover:bg-indigo-50/60 transition duration-150"
                        title="重命名会话"
                      >
                        <Edit2 className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onToggleStar) onToggleStar(session.id);
                        }}
                        className={`p-1 rounded transition duration-150 ${
                          session.isStarred 
                            ? 'text-amber-500 hover:bg-amber-100/60' 
                            : 'text-slate-300 hover:text-slate-500 hover:bg-slate-100'
                        }`}
                        title={session.isStarred ? "取消标星" : "标星为重点营销项目"}
                      >
                        <Star className={`h-3.5 w-3.5 ${session.isStarred ? 'fill-amber-400' : ''}`} />
                      </button>
                      <span className="text-[10px] opacity-70 ml-0.5">
                        {session.id === "WO-1001" ? "2分钟前" : session.id === "WO-1002" ? "4小时前" : "1天前"}
                      </span>
                    </div>
                  </div>
                )}

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
      <div className="p-4 border-t border-slate-100 bg-white shrink-0 space-y-3 relative">
        
        {/* User Account Info Info */}
        {user && (
          <div className="flex items-center justify-between p-2 rounded-xl bg-slate-50 border border-slate-100/70 mb-1.5">
            <div className="flex items-center gap-2 min-w-0">
              <div className="w-8 h-8 rounded-full bg-indigo-600/10 text-indigo-700 font-bold flex items-center justify-center text-xs shrink-0 select-none uppercase">
                {user.nickname.charAt(0)}
              </div>
              <div className="min-w-0">
                <p className="text-[11px] font-bold text-slate-800 truncate" title={user.nickname}>
                  {user.nickname}
                </p>
                <span className="text-[9px] text-slate-400 font-medium flex items-center gap-1">
                  {user.loginMethod === 'sms' ? '📱 手机验证登录' : '💬 微信扫码登录'}
                </span>
                <button
                  onClick={onOpenAdmin}
                  className="text-[9px] text-indigo-600 font-bold bg-indigo-50 border border-indigo-100/50 px-1.5 py-0.5 rounded flex items-center gap-0.5 mt-1 hover:bg-indigo-100 transition active:scale-95"
                >
                  <Shield className="h-2 w-2" />
                  系统管理后台
                </button>
              </div>
            </div>
            {onLogout && (
              <button
                onClick={onLogout}
                className="p-1.5 rounded-lg text-slate-400 hover:text-rose-600 hover:bg-rose-50 transition-all duration-150 active:scale-95 shrink-0"
                title="退出登录"
              >
                <LogOut className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        )}

        <div className="flex items-center justify-between">
          <div className="flex flex-col">
            <span className="text-[10px] font-bold text-slate-400">积分额度</span>
            <span className="text-xs font-bold text-slate-700 font-display">
              {points.toLocaleString()} / {maxPoints.toLocaleString()} 点
            </span>
          </div>
          <button
            onClick={onOpenRecharge}
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
      </div>

    </div>
  );
}
