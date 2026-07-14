import React, { useState } from 'react';
import { 
  Users, UserPlus, Shield, Smartphone, Coins, Check, X, Edit, Trash2, 
  Search, CheckCircle2, AlertCircle, Sparkles, Filter, RefreshCw,
  BarChart3, History, Calendar, TrendingUp
} from 'lucide-react';
import { 
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, 
  BarChart, Bar, Cell, PieChart, Pie, Legend
} from 'recharts';
import { Account } from '../types';

interface AdminPanelProps {
  isOpen: boolean;
  readOnly?: boolean;
  onClose: () => void;
  accounts: Account[];
  onUpdateAccounts: (accounts: Account[]) => void;
  currentUserPhone?: string;
  currentUserNickname?: string;
}

export interface PointsHistoryEntry {
  id: string;
  sessionTitle: string;
  date: string;       // "YYYY-MM-DD"
  points: number;     // points consumed
  platform: string;   // e.g. "小红书", "抖音"
}

export const getPointsHistoryForAccount = (accId: string): PointsHistoryEntry[] => {
  if (accId === 'acc-1') {
    return [
      { id: 'h1', sessionTitle: '完美日记-丝绒雾面系列发布', date: '2026-07-12', points: 450, platform: '小红书' },
      { id: 'h2', sessionTitle: '雅诗兰黛-双胶原晚霜推广', date: '2026-07-10', points: 300, platform: '抖音' },
      { id: 'h3', sessionTitle: '安克创新-数码出海社媒监控', date: '2026-07-05', points: 500, platform: 'YouTube' },
      { id: 'h4', sessionTitle: '花西子-东方彩妆海外众筹', date: '2026-06-20', points: 600, platform: 'Instagram' },
      { id: 'h5', sessionTitle: '三只松鼠-中秋礼盒爆款打造', date: '2026-06-12', points: 200, platform: '微博' },
      { id: 'h6', sessionTitle: '李宁-潮流国风卫衣矩阵', date: '2026-05-18', points: 400, platform: 'B站' },
      { id: 'h7', sessionTitle: '完美日记-心愿眼影盘种草', date: '2025-12-15', points: 350, platform: '小红书' },
      { id: 'h8', sessionTitle: '蔚来汽车-新车型KOL声量会话', date: '2025-10-08', points: 800, platform: '微信' },
    ];
  }
  if (accId === 'acc-2') {
    return [
      { id: 'h21', sessionTitle: '安克创新-无线充电桩首发', date: '2026-07-11', points: 300, platform: '抖音' },
      { id: 'h22', sessionTitle: 'Anker-氮化镓充电器大促', date: '2026-07-09', points: 250, platform: 'B站' },
      { id: 'h23', sessionTitle: '安克创新-户外电源野营场景', date: '2026-06-25', points: 400, platform: '小红书' },
      { id: 'h24', sessionTitle: 'Anker-降噪耳机运动博主投放', date: '2026-05-10', points: 500, platform: '抖音' },
      { id: 'h25', sessionTitle: 'Anker-车载快充极客评测', date: '2025-11-20', points: 150, platform: 'B站' },
    ];
  }
  if (accId === 'acc-3') {
    return [
      { id: 'h31', sessionTitle: '花西子-玉容散气垫推广', date: '2026-07-13', points: 450, platform: '小红书' },
      { id: 'h32', sessionTitle: '花西子-雕花口红七夕限定', date: '2026-07-08', points: 300, platform: '微博' },
      { id: 'h33', sessionTitle: '花西子-空气蜜粉周年庆', date: '2026-06-18', points: 250, platform: '小红书' },
    ];
  }
  
  // For other users, generate a deterministic history based on their id string
  const numId = parseInt(accId.replace(/\D/g, '')) || 999;
  return [
    { id: `${accId}-1`, sessionTitle: 'AI智能对话-常规诊断分析', date: '2026-07-13', points: 200 + (numId % 200), platform: '小红书' },
    { id: `${accId}-2`, sessionTitle: '渠道声量-交叉基准比对', date: '2026-07-08', points: 150 + (numId % 150), platform: '抖音' },
    { id: `${accId}-3`, sessionTitle: 'KOL匹配与舆情口碑评测', date: '2026-06-15', points: 300 + (numId % 300), platform: 'B站' },
  ];
};

const getChartData = (entries: PointsHistoryEntry[], filterType: 'day' | 'month' | 'year') => {
  const groups: { [key: string]: number } = {};
  
  entries.forEach(entry => {
    let key = '';
    const dateParts = entry.date.split('-'); // ["2026", "07", "12"]
    if (filterType === 'day') {
      key = entry.date;
    } else if (filterType === 'month') {
      key = `${dateParts[0]}-${dateParts[1]}`; // "YYYY-MM"
    } else if (filterType === 'year') {
      key = dateParts[0]; // "YYYY"
    }
    
    groups[key] = (groups[key] || 0) + entry.points;
  });

  return Object.keys(groups)
    .sort()
    .map(key => {
      let displayName = key;
      if (filterType === 'day') {
        const parts = key.split('-');
        if (parts.length === 3) displayName = `${parts[1]}/${parts[2]}`; // "07/12"
      } else if (filterType === 'month') {
        const parts = key.split('-');
        if (parts.length === 2) displayName = `${parts[0].slice(2)}年${parts[1]}月`; // "26年07月"
      } else if (filterType === 'year') {
        displayName = `${key}年`;
      }
      return {
        name: displayName,
        rawKey: key,
        points: groups[key]
      };
    });
};

const AVAILABLE_CHANNELS = ["小红书", "抖音", "B站", "微博", "YouTube", "Instagram"];

export default function AdminPanel({
  isOpen,
  readOnly = false,
  onClose,
  accounts,
  onUpdateAccounts,
  currentUserPhone,
  currentUserNickname
}: AdminPanelProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedFilterChannels, setSelectedFilterChannels] = useState<string[]>([]);
  const [isFilterDropdownOpen, setIsFilterDropdownOpen] = useState(false);
  const [isAdding, setIsAdding] = useState(false);
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [selectedHistoryUser, setSelectedHistoryUser] = useState<Account | null>(null);
  const [historyFilterType, setHistoryFilterType] = useState<'day' | 'month' | 'year'>('day');

  // Form states for Add/Edit
  const [formUsername, setFormUsername] = useState('');
  const [formPhone, setFormPhone] = useState('');
  const [formChannels, setFormChannels] = useState<string[]>([]);
  const [formPoints, setFormPoints] = useState(2000);
  const [formRole, setFormRole] = useState<'admin' | 'user'>('user');
  const [errorMsg, setErrorMsg] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  if (!isOpen) return null;

  if (readOnly) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4">
        <div className="w-full max-w-lg overflow-hidden rounded-2xl border border-slate-100 bg-white shadow-2xl animate-in fade-in zoom-in-95 duration-200">
          <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/50 px-6 py-4">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-50 text-indigo-600 shadow-inner">
                <Shield className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-base font-bold text-slate-800 font-display">系统管理员控制台</h2>
                <p className="text-[10px] font-medium text-slate-400">当前基础阶段仅开放安全预览</p>
              </div>
            </div>
            <button
              onClick={onClose}
              className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="flex flex-col items-center px-8 py-12 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-slate-100 text-slate-400">
              <Users className="h-7 w-7 stroke-[1.5]" />
            </div>
            <h3 className="mt-4 text-sm font-bold text-slate-800">系统管理暂为只读</h3>
            <p className="mt-2 max-w-sm text-xs leading-relaxed text-slate-400">
              用户、渠道权限与积分审计将在管理 API 完成后接入。当前页面不加载模拟账户，也不能修改用户或积分。
            </p>
            <span className="mt-4 rounded-lg border border-indigo-100 bg-indigo-50 px-2.5 py-1 text-[10px] font-bold text-indigo-600">
              当前身份：{currentUserNickname || '系统管理员'}
            </span>
          </div>

          <div className="flex justify-end border-t border-slate-100 bg-slate-50 px-6 py-3.5">
            <button
              onClick={onClose}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-indigo-700 active:scale-[0.98]"
            >
              返回工作区
            </button>
          </div>
        </div>
      </div>
    );
  }

  const handleOpenAdd = () => {
    setFormUsername('');
    setFormPhone('');
    setFormChannels(["小红书", "抖音"]);
    setFormPoints(2000);
    setFormRole('user');
    setErrorMsg('');
    setSuccessMsg('');
    setIsAdding(true);
    setEditingAccount(null);
    setSelectedHistoryUser(null);
  };

  const handleOpenEdit = (acc: Account) => {
    setEditingAccount(acc);
    setFormUsername(acc.username);
    setFormPhone(acc.phone);
    setFormChannels(acc.channels);
    setFormPoints(acc.points);
    setFormRole(acc.role);
    setErrorMsg('');
    setSuccessMsg('');
    setIsAdding(false);
    setSelectedHistoryUser(null);
  };

  const handleOpenHistory = (acc: Account) => {
    setSelectedHistoryUser(acc);
    setHistoryFilterType('day');
    setIsAdding(false);
    setEditingAccount(null);
  };

  const handleToggleChannel = (channel: string) => {
    setFormChannels(prev => 
      prev.includes(channel) 
        ? prev.filter(c => c !== channel) 
        : [...prev, channel]
    );
  };

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg('');
    setSuccessMsg('');

    if (!formUsername.trim()) {
      setErrorMsg('用户名不能为空');
      return;
    }

    const phoneRegex = /^1[3-9]\d{9}$/;
    if (!phoneRegex.test(formPhone)) {
      setErrorMsg('请输入有效的11位手机号码');
      return;
    }

    if (formChannels.length === 0) {
      setErrorMsg('请至少选择一个渠道权限');
      return;
    }

    if (formPoints < 0 || formPoints > 50000) {
      setErrorMsg('积分余额须在 0 到 50,000 之间');
      return;
    }

    if (isAdding) {
      // Check if phone already exists
      if (accounts.some(a => a.phone === formPhone)) {
        setErrorMsg('该手机号已注册过账号');
        return;
      }

      const newAcc: Account = {
        id: `acc-${Date.now()}`,
        username: formUsername.trim(),
        phone: formPhone,
        channels: formChannels,
        points: formPoints,
        role: formRole,
        createdAt: new Date().toLocaleDateString('zh-CN')
      };

      onUpdateAccounts([...accounts, newAcc]);
      setSuccessMsg(`账号「${formUsername}」创建成功！`);
      setTimeout(() => {
        setIsAdding(false);
        setSuccessMsg('');
      }, 1500);

    } else if (editingAccount) {
      // Edit mode
      // Check duplicate phone except self
      if (accounts.some(a => a.phone === formPhone && a.id !== editingAccount.id)) {
        setErrorMsg('该手机号已被其他账号使用');
        return;
      }

      const updated = accounts.map(a => 
        a.id === editingAccount.id 
          ? { 
              ...a, 
              username: formUsername.trim(), 
              phone: formPhone, 
              channels: formChannels, 
              points: formPoints,
              role: formRole
            }
          : a
      );

      onUpdateAccounts(updated);
      setSuccessMsg(`账号「${formUsername}」修改成功！`);
      setTimeout(() => {
        setEditingAccount(null);
        setSuccessMsg('');
      }, 1500);
    }
  };

  const handleDelete = (id: string, name: string) => {
    if (confirm(`确认要删除账号「${name}」吗？`)) {
      onUpdateAccounts(accounts.filter(a => a.id !== id));
    }
  };

  const handleResetData = () => {
    if (confirm('确认要重置账号列表为系统默认演示数据吗？')) {
      const defaultAccounts: Account[] = [
        {
          id: 'acc-1',
          username: '系统超级管理员',
          phone: '18888888888',
          channels: ["小红书", "抖音", "B站", "微博", "YouTube", "Instagram"],
          points: 5000,
          role: 'admin',
          createdAt: '2026-01-01'
        },
        {
          id: 'acc-2',
          username: '手机用户_Anker',
          phone: '13812345678',
          channels: ["小红书", "抖音", "B站"],
          points: 3450,
          role: 'user',
          createdAt: '2026-06-15'
        },
        {
          id: 'acc-3',
          username: '微信快捷登录用户',
          phone: '13900001111',
          channels: ["小红书", "微博"],
          points: 1200,
          role: 'user',
          createdAt: '2026-07-01'
        }
      ];
      onUpdateAccounts(defaultAccounts);
    }
  };

  const filteredAccounts = accounts.filter(acc => {
    const matchesSearch = 
      acc.username.toLowerCase().includes(searchQuery.toLowerCase()) ||
      acc.phone.includes(searchQuery);
    
    const matchesChannel = 
      selectedFilterChannels.length === 0 || 
      acc.channels.some(ch => selectedFilterChannels.includes(ch));

    return matchesSearch && matchesChannel;
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4 overflow-y-auto">
      <div className={`bg-white rounded-2xl border border-slate-100 shadow-2xl w-full max-h-[90vh] flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200 transition-all ${
        selectedHistoryUser ? 'max-w-5xl' : 'max-w-4xl'
      }`}>
        
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-xl bg-indigo-50 flex items-center justify-center text-indigo-600 shadow-inner">
              <Shield className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-800 font-display">系统管理员控制台</h2>
              <p className="text-[10px] text-slate-400 font-medium">配置多用户渠道权限、增删账号、自由调配账号积分余额</p>
            </div>
          </div>
          <button 
            onClick={onClose}
            className="h-8 w-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Content Area */}
        <div className="flex-1 overflow-y-auto flex flex-col md:flex-row min-h-0">
          
          {/* Left panel: List of accounts */}
          <div className="flex-1 p-6 border-r border-slate-100 flex flex-col min-h-0 space-y-4">
            
            {/* Filter & Add Actions */}
            <div className="flex flex-col sm:flex-row gap-2 justify-between">
              <div className="flex items-center gap-2 flex-1 max-w-md">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-slate-400" />
                  <input
                    type="text"
                    placeholder="按用户名、手机号搜索..."
                    value={searchQuery}
                    onChange={e => setSearchQuery(e.target.value)}
                    className="w-full bg-slate-50 border border-slate-200 rounded-lg py-1.5 pl-9 pr-3 text-xs text-slate-700 outline-none focus:border-indigo-500 focus:bg-white transition"
                  />
                </div>
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setIsFilterDropdownOpen(!isFilterDropdownOpen)}
                    className="flex items-center gap-1.5 bg-slate-50 hover:bg-slate-100 border border-slate-200 rounded-lg py-1.5 px-2.5 text-xs text-slate-600 transition"
                  >
                    <Filter className="h-3.5 w-3.5 text-slate-400" />
                    <span>
                      {selectedFilterChannels.length === 0 
                        ? '所有渠道权限' 
                        : `已选渠道 (${selectedFilterChannels.length})`
                      }
                    </span>
                  </button>

                  {isFilterDropdownOpen && (
                    <>
                      <div 
                        className="fixed inset-0 z-10" 
                        onClick={() => setIsFilterDropdownOpen(false)} 
                      />
                      <div className="absolute right-0 mt-1.5 w-44 bg-white rounded-xl border border-slate-100 shadow-xl p-1.5 z-20 space-y-0.5 animate-in fade-in slide-in-from-top-1 duration-150 text-[11px] max-h-60 overflow-y-auto">
                        <div className="px-2 py-1 border-b border-slate-100 flex items-center justify-between text-[9px] font-bold text-slate-400">
                          <span>渠道多选筛选</span>
                          {selectedFilterChannels.length > 0 && (
                            <button 
                              onClick={(e) => {
                                e.stopPropagation();
                                setSelectedFilterChannels([]);
                              }}
                              className="text-indigo-600 hover:text-indigo-800"
                            >
                              重置
                            </button>
                          )}
                        </div>
                        {AVAILABLE_CHANNELS.map(ch => {
                          const isSelected = selectedFilterChannels.includes(ch);
                          return (
                            <button
                              key={ch}
                              type="button"
                              onClick={() => {
                                setSelectedFilterChannels(prev => 
                                  isSelected 
                                    ? prev.filter(c => c !== ch) 
                                    : [...prev, ch]
                                );
                              }}
                              className={`w-full py-1 px-2 rounded-md text-left font-medium transition flex items-center justify-between hover:bg-slate-50 ${
                                isSelected ? 'bg-indigo-50/50 text-indigo-600 font-semibold' : 'text-slate-600'
                              }`}
                            >
                              <span>{ch}</span>
                              {isSelected && <Check className="h-3.5 w-3.5 text-indigo-600 shrink-0" />}
                            </button>
                          );
                        })}
                      </div>
                    </>
                  )}
                </div>
              </div>

              <div className="flex gap-1.5 shrink-0">
                <button
                  onClick={handleResetData}
                  className="px-3 py-1.5 text-xs font-bold text-slate-500 hover:text-indigo-600 bg-white hover:bg-indigo-50 border border-slate-200 hover:border-indigo-100 rounded-lg transition flex items-center gap-1 active:scale-95"
                  title="重置测试数据"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  <span>重置</span>
                </button>
                <button
                  onClick={handleOpenAdd}
                  className="px-3.5 py-1.5 text-xs font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-sm transition flex items-center gap-1.5 active:scale-95"
                >
                  <UserPlus className="h-3.5 w-3.5" />
                  <span>新增账号</span>
                </button>
              </div>
            </div>

            {/* Account List Grid */}
            <div className="flex-1 overflow-y-auto border border-slate-100 rounded-xl bg-slate-50/20 max-h-[50vh] md:max-h-none">
              {filteredAccounts.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 text-slate-400">
                  <Users className="h-10 w-10 text-slate-300 mb-2 stroke-[1.5]" />
                  <p className="text-xs">未找到符合条件的账号记录</p>
                </div>
              ) : (
                <div className="divide-y divide-slate-100">
                  {filteredAccounts.map(acc => {
                    const isSelf = currentUserPhone === acc.phone;
                    return (
                      <div 
                        key={acc.id} 
                        className={`p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 hover:bg-indigo-50/10 transition duration-150 ${
                          editingAccount?.id === acc.id ? 'bg-indigo-50/25 border-l-2 border-indigo-500' : 
                          selectedHistoryUser?.id === acc.id ? 'bg-amber-50/25 border-l-2 border-amber-500' : ''
                        }`}
                      >
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-bold text-xs text-slate-800">{acc.username}</span>
                            {acc.role === 'admin' ? (
                              <span className="text-[9px] font-extrabold px-1.5 py-0.2 rounded bg-amber-50 text-amber-600 border border-amber-200/50 flex items-center gap-0.5">
                                <Shield className="h-2 w-2" />
                                管理员
                              </span>
                            ) : (
                              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-slate-100 text-slate-500 border border-slate-200/50">
                                渠道分析师
                              </span>
                            )}
                            {isSelf && (
                              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-indigo-500 text-white">
                                当前登录
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-3 text-[10.5px] text-slate-500">
                            <span className="flex items-center gap-1 font-mono">
                              <Smartphone className="h-3 w-3 text-slate-400" />
                              {acc.phone}
                            </span>
                            <span className="flex items-center gap-1 font-semibold text-slate-700">
                              <Coins className="h-3 w-3 text-amber-500" />
                              积分: {acc.points.toLocaleString()}
                            </span>
                          </div>
                          
                          {/* Channel Permits */}
                          <div className="flex flex-wrap gap-1 pt-1.5">
                            {acc.channels.map(ch => (
                              <span 
                                key={ch} 
                                className="text-[9px] font-semibold px-1.5 py-0.2 bg-indigo-50 text-indigo-600 rounded border border-indigo-100/40"
                              >
                                {ch}
                              </span>
                            ))}
                          </div>
                        </div>

                        {/* Actions */}
                        <div className="flex items-center gap-1.5 self-end sm:self-center">
                          <button
                            onClick={() => handleOpenHistory(acc)}
                            className={`p-1.5 rounded-lg transition ${
                              selectedHistoryUser?.id === acc.id
                                ? 'text-amber-600 bg-amber-50'
                                : 'text-slate-500 hover:text-amber-600 hover:bg-amber-50'
                            }`}
                            title="查看历史营销会话积分消耗状况"
                          >
                            <BarChart3 className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => handleOpenEdit(acc)}
                            className="p-1.5 rounded-lg text-slate-500 hover:text-indigo-600 hover:bg-indigo-50 transition"
                            title="管理/修改此账号信息"
                          >
                            <Edit className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => handleDelete(acc.id, acc.username)}
                            disabled={isSelf}
                            className={`p-1.5 rounded-lg transition ${
                              isSelf 
                                ? 'text-slate-200 cursor-not-allowed' 
                                : 'text-slate-400 hover:text-rose-600 hover:bg-rose-50'
                            }`}
                            title={isSelf ? "无法删除当前登录的主管理员账号" : "删除账号"}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {/* Right panel: Editor / Creator */}
          {(isAdding || editingAccount) ? (
            <div className="w-full md:w-80 p-6 bg-slate-50/50 flex flex-col shrink-0 border-t md:border-t-0 md:border-l border-slate-100 animate-in slide-in-from-right-10 duration-200">
              <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-4">
                <h3 className="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                  <Sparkles className="h-3.5 w-3.5 text-indigo-500" />
                  {isAdding ? '新增成员账号' : '修改/设置账号信息'}
                </h3>
                <button 
                  onClick={() => { setIsAdding(false); setEditingAccount(null); }}
                  className="text-slate-400 hover:text-slate-600 text-xs font-bold"
                >
                  关闭
                </button>
              </div>

              <form onSubmit={handleSave} className="space-y-4 text-xs">
                {errorMsg && (
                  <div className="flex items-center gap-1.5 bg-rose-50 border border-rose-100 p-2.5 rounded-lg text-rose-600 font-medium">
                    <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                    <span>{errorMsg}</span>
                  </div>
                )}

                {successMsg && (
                  <div className="flex items-center gap-1.5 bg-emerald-50 border border-emerald-100 p-2.5 rounded-lg text-emerald-700 font-medium animate-bounce">
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                    <span>{successMsg}</span>
                  </div>
                )}

                {/* Username */}
                <div className="space-y-1">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">用户名 / 成员称呼</label>
                  <input
                    type="text"
                    required
                    placeholder="例如: 完美日记运营"
                    value={formUsername}
                    onChange={e => setFormUsername(e.target.value)}
                    className="w-full bg-white border border-slate-200 rounded-lg py-2 px-2.5 text-slate-700 outline-none focus:border-indigo-500 transition"
                  />
                </div>

                {/* Phone */}
                <div className="space-y-1">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">手机号码 (作为登录凭证)</label>
                  <input
                    type="tel"
                    required
                    maxLength={11}
                    placeholder="11位手机号码"
                    value={formPhone}
                    onChange={e => setFormPhone(e.target.value.replace(/\D/g, ''))}
                    className="w-full bg-white border border-slate-200 rounded-lg py-2 px-2.5 text-slate-700 font-mono outline-none focus:border-indigo-500 transition"
                  />
                </div>

                {/* Points */}
                <div className="space-y-1">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">设置积分余额</label>
                  <div className="flex gap-1.5 items-center">
                    <input
                      type="number"
                      required
                      min={0}
                      max={50000}
                      placeholder="设置可用积分上限"
                      value={formPoints}
                      onChange={e => setFormPoints(Math.max(0, parseInt(e.target.value, 10) || 0))}
                      className="flex-1 bg-white border border-slate-200 rounded-lg py-2 px-2.5 text-slate-700 font-mono outline-none focus:border-indigo-500 transition font-bold"
                    />
                    <div className="flex gap-1 shrink-0">
                      <button
                        type="button"
                        onClick={() => setFormPoints(prev => Math.min(50000, prev + 500))}
                        className="px-2 py-1 bg-white hover:bg-slate-100 border border-slate-200 text-[10px] font-bold text-slate-600 rounded"
                      >
                        +500
                      </button>
                      <button
                        type="button"
                        onClick={() => setFormPoints(1000)}
                        className="px-2 py-1 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 text-[10px] font-bold text-indigo-700 rounded"
                        title="快速设为1000"
                      >
                        1k
                      </button>
                      <button
                        type="button"
                        onClick={() => setFormPoints(5000)}
                        className="px-2 py-1 bg-amber-50 hover:bg-amber-100 border border-amber-200 text-[10px] font-bold text-amber-700 rounded"
                        title="快速设为满额"
                      >
                        5k
                      </button>
                    </div>
                  </div>
                </div>

                {/* Role */}
                <div className="space-y-1">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">系统角色</label>
                  <div className="flex gap-2">
                    <label className="flex-1 flex items-center gap-1.5 p-2 bg-white border border-slate-200 rounded-lg cursor-pointer hover:bg-indigo-50/10 transition">
                      <input
                        type="radio"
                        name="formRole"
                        checked={formRole === 'user'}
                        onChange={() => setFormRole('user')}
                        className="text-indigo-600 focus:ring-indigo-500 h-3.5 w-3.5"
                      />
                      <span>渠道分析师</span>
                    </label>
                    <label className="flex-1 flex items-center gap-1.5 p-2 bg-white border border-slate-200 rounded-lg cursor-pointer hover:bg-indigo-50/10 transition">
                      <input
                        type="radio"
                        name="formRole"
                        checked={formRole === 'admin'}
                        onChange={() => setFormRole('admin')}
                        className="text-indigo-600 focus:ring-indigo-500 h-3.5 w-3.5"
                      />
                      <span>管理员</span>
                    </label>
                  </div>
                </div>

                {/* Channel Permissions checkboxes */}
                <div className="space-y-1.5">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">渠道分析权限</label>
                  <div className="grid grid-cols-2 gap-1.5">
                    {AVAILABLE_CHANNELS.map(ch => {
                      const isSelected = formChannels.includes(ch);
                      return (
                        <button
                          key={ch}
                          type="button"
                          onClick={() => handleToggleChannel(ch)}
                          className={`py-1.5 px-2 rounded-lg border text-left font-medium transition flex items-center justify-between ${
                            isSelected 
                              ? 'bg-indigo-50 border-indigo-200 text-indigo-700 font-semibold' 
                              : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
                          }`}
                        >
                          <span>{ch}</span>
                          {isSelected && <Check className="h-3 w-3 text-indigo-600 shrink-0" />}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* Submit buttons */}
                <div className="pt-3 border-t border-slate-100 flex gap-2">
                  <button
                    type="button"
                    onClick={() => { setIsAdding(false); setEditingAccount(null); }}
                    className="flex-1 py-2 bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold rounded-lg transition text-center"
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="flex-1 py-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold rounded-lg shadow-sm transition text-center"
                  >
                    {isAdding ? '立即添加' : '保存修改'}
                  </button>
                </div>
              </form>
            </div>
          ) : selectedHistoryUser ? (
            <div className="w-full md:w-[420px] p-6 bg-slate-50/50 flex flex-col shrink-0 border-t md:border-t-0 md:border-l border-slate-100 animate-in slide-in-from-right-10 duration-200 min-h-0 overflow-y-auto">
              <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-4">
                <div className="flex items-center gap-1.5">
                  <History className="h-4 w-4 text-amber-500 shrink-0" />
                  <span className="text-xs font-bold text-slate-800 font-display">
                    「{selectedHistoryUser.username}」积分消耗分析
                  </span>
                </div>
                <button 
                  onClick={() => setSelectedHistoryUser(null)}
                  className="text-slate-400 hover:text-slate-600 text-xs font-bold"
                >
                  关闭
                </button>
              </div>

              {/* Day/Month/Year Switcher */}
              <div className="mb-4 bg-slate-100 p-0.5 rounded-lg flex text-xs">
                {(['day', 'month', 'year'] as const).map(type => (
                  <button
                    key={type}
                    type="button"
                    onClick={() => setHistoryFilterType(type)}
                    className={`flex-1 py-1.5 text-center font-bold rounded-md transition ${
                      historyFilterType === type 
                        ? 'bg-white text-slate-800 shadow-xs' 
                        : 'text-slate-400 hover:text-slate-600'
                    }`}
                  >
                    {type === 'day' ? '按天' : type === 'month' ? '按月' : '按年'}
                  </button>
                ))}
              </div>

              {/* Chart Section */}
              <div className="bg-white border border-slate-100 rounded-xl p-4 shadow-xs space-y-3 mb-4">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                    消耗趋势 ({historyFilterType === 'day' ? '日趋势' : historyFilterType === 'month' ? '月趋势' : '年趋势'})
                  </span>
                  <div className="flex items-center gap-1 text-[11px] font-semibold text-amber-600 bg-amber-50 px-2 py-0.5 rounded-md">
                    <Coins className="h-3 w-3" />
                    <span>总消耗: {getPointsHistoryForAccount(selectedHistoryUser.id).reduce((sum, item) => sum + item.points, 0).toLocaleString()} 积分</span>
                  </div>
                </div>

                <div className="h-44 w-full text-xs">
                  {getPointsHistoryForAccount(selectedHistoryUser.id).length === 0 ? (
                    <div className="h-full flex flex-col items-center justify-center text-slate-400">
                      <BarChart3 className="h-8 w-8 text-slate-300 stroke-[1.5] mb-1.5" />
                      <p className="text-[10.5px]">暂无历史消耗数据</p>
                    </div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart 
                        data={getChartData(getPointsHistoryForAccount(selectedHistoryUser.id), historyFilterType)}
                        margin={{ top: 10, right: 10, left: -25, bottom: 0 }}
                      >
                        <XAxis 
                          dataKey="name" 
                          stroke="#94a3b8" 
                          fontSize={9} 
                          tickLine={false} 
                          axisLine={false}
                        />
                        <YAxis 
                          stroke="#94a3b8" 
                          fontSize={9} 
                          tickLine={false} 
                          axisLine={false}
                        />
                        <Tooltip 
                          content={({ active, payload }) => {
                            if (active && payload && payload.length) {
                              return (
                                <div className="bg-slate-900 text-white text-[10px] p-2 rounded-lg shadow-md font-sans border border-slate-800">
                                  <p className="font-bold border-b border-slate-800 pb-1 mb-1">
                                    {payload[0].payload.rawKey || payload[0].payload.name}
                                  </p>
                                  <p className="flex items-center gap-1 font-medium text-amber-400">
                                    <Coins className="h-3 w-3 text-amber-400" />
                                    <span>消耗: {payload[0].value} 积分</span>
                                  </p>
                                </div>
                              );
                            }
                            return null;
                          }}
                        />
                        <Bar 
                          dataKey="points" 
                          fill="#4f46e5" 
                          radius={[4, 4, 0, 0]}
                        >
                          {getChartData(getPointsHistoryForAccount(selectedHistoryUser.id), historyFilterType).map((entry, idx) => (
                            <Cell 
                              key={`cell-${idx}`} 
                              fill={idx % 2 === 0 ? '#4f46e5' : '#818cf8'} 
                            />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  )}
                </div>
              </div>

              {/* Ledger List */}
              <div className="flex-1 min-h-0 flex flex-col space-y-2">
                <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider block">
                  消费明细流水 (历史营销会话)
                </span>
                <div className="flex-1 overflow-y-auto border border-slate-100 rounded-xl bg-white divide-y divide-slate-50 p-1">
                  {getPointsHistoryForAccount(selectedHistoryUser.id).map(entry => (
                    <div key={entry.id} className="p-2.5 flex items-center justify-between text-xs hover:bg-slate-50 rounded-lg transition duration-150">
                      <div className="space-y-1 min-w-0 pr-2">
                        <p className="font-bold text-slate-700 truncate text-[11px]" title={entry.sessionTitle}>
                          {entry.sessionTitle}
                        </p>
                        <div className="flex items-center gap-2 text-[10px] text-slate-400 font-medium">
                          <span className="flex items-center gap-0.5 bg-indigo-50/50 text-indigo-600 px-1 py-0.2 rounded border border-indigo-100/20 shrink-0">
                            {entry.platform}
                          </span>
                          <span className="flex items-center gap-0.5">
                            <Calendar className="h-2.5 w-2.5" />
                            {entry.date}
                          </span>
                        </div>
                      </div>
                      <div className="text-right shrink-0">
                        <span className="text-[11px] font-extrabold text-rose-600 bg-rose-50 border border-rose-100 px-1.5 py-0.5 rounded font-mono">
                          -{entry.points}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="hidden md:flex w-80 p-6 bg-slate-50/50 flex-col items-center justify-center text-center shrink-0 border-l border-slate-100">
              <div className="h-12 w-12 rounded-full bg-slate-100 flex items-center justify-center text-slate-400 mb-3">
                <Users className="h-6 w-6 stroke-[1.5]" />
              </div>
              <h4 className="font-bold text-xs text-slate-700">未选择任何操作</h4>
              <p className="text-[10.5px] text-slate-400 mt-1 max-w-[180px] leading-relaxed">
                点击左侧列表账号的 <Edit className="h-3 w-3 inline text-slate-400" /> 进行修改与配置，点击 <BarChart3 className="h-3 w-3 inline text-amber-500" /> 查看积分消耗，或点击「新增账号」添加新账号。
              </p>
            </div>
          )}

        </div>

        {/* Footer info banner */}
        <div className="bg-slate-50 border-t border-slate-100 px-6 py-3.5 flex items-center justify-between text-[11px] text-slate-500 shrink-0">
          <span className="flex items-center gap-1 font-medium">
            <CheckCircle2 className="h-3.5 w-3.5 text-indigo-500" />
            系统管理员模式已安全挂载：您正在以 <strong>{currentUserNickname || '超级管理员'}</strong> 身份管理节点
          </span>
          <button 
            onClick={onClose}
            className="text-xs font-bold text-indigo-600 hover:text-indigo-800 transition"
          >
            完成配置并返回
          </button>
        </div>

      </div>
    </div>
  );
}
