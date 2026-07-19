import React, { useCallback, useEffect, useState } from 'react';
import {
  Users, UserPlus, Shield, Smartphone, Coins, Check, X, Edit, Trash2,
  Search, CheckCircle2, AlertCircle, Sparkles, Filter,
  BarChart3, History, Calendar
} from 'lucide-react';
import {
  ResponsiveContainer, XAxis, YAxis, Tooltip,
  BarChart, Bar, Cell
} from 'recharts';
import type { ApiAdminUser, ApiPointsHistoryEntry } from '../api/contracts';
import {
  adjustAdminUserPoints,
  createAdminUser,
  deleteAdminUser,
  getAdminUserPointsHistory,
  listAdminUsers,
  updateAdminUser,
} from '../api/admin';

interface AdminPanelProps {
  isOpen: boolean;
  onClose: () => void;
  currentUserId: string;
  currentUserNickname?: string;
}

const CHANNEL_OPTIONS = [
  { slug: 'xiaohongshu', label: '小红书' },
  { slug: 'douyin', label: '抖音' },
  { slug: 'bilibili', label: 'B站' },
  { slug: 'weibo', label: '微博' },
  { slug: 'wechat', label: '微信' },
];

const channelLabel = (slug: string): string =>
  CHANNEL_OPTIONS.find(c => c.slug === slug)?.label ?? slug;

interface HistoryDisplayEntry {
  id: string;
  kind: string;
  title: string;
  platformLabel: string | null;
  date: string;       // "YYYY-MM-DD"
  points: number;
}

const KIND_BADGES: Record<string, { label: string; className: string }> = {
  settle: { label: '消费', className: 'bg-rose-50 text-rose-600 border-rose-100' },
  welcome_grant: { label: '赠送', className: 'bg-emerald-50 text-emerald-600 border-emerald-100' },
  admin_adjust: { label: '调整', className: 'bg-amber-50 text-amber-600 border-amber-100' },
};

const mapHistoryEntry = (entry: ApiPointsHistoryEntry): HistoryDisplayEntry => {
  let title = entry.session_title ?? entry.kind;
  if (entry.kind === 'settle') title = entry.session_title ?? '未知会话';
  else if (entry.kind === 'welcome_grant') title = '新人积分赠送';
  else if (entry.kind === 'admin_adjust') title = '管理员积分调整';
  return {
    id: entry.id,
    kind: entry.kind,
    title,
    platformLabel: entry.platform ? channelLabel(entry.platform) : null,
    date: entry.created_at.slice(0, 10),
    points: entry.points,
  };
};

const getChartData = (entries: HistoryDisplayEntry[], filterType: 'day' | 'month' | 'year') => {
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

const getErrorMessage = (err: unknown, fallback: string): string =>
  err instanceof Error && err.message ? err.message : fallback;

export default function AdminPanel({
  isOpen,
  onClose,
  currentUserId,
  currentUserNickname
}: AdminPanelProps) {
  const [users, setUsers] = useState<ApiAdminUser[]>([]);
  const [isListLoading, setIsListLoading] = useState(false);
  const [listError, setListError] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedFilterChannel, setSelectedFilterChannel] = useState('');
  const [isFilterDropdownOpen, setIsFilterDropdownOpen] = useState(false);
  const [isAdding, setIsAdding] = useState(false);
  const [editingAccount, setEditingAccount] = useState<ApiAdminUser | null>(null);
  const [selectedHistoryUser, setSelectedHistoryUser] = useState<ApiAdminUser | null>(null);
  const [historyFilterType, setHistoryFilterType] = useState<'day' | 'month' | 'year'>('day');
  const [historyEntries, setHistoryEntries] = useState<HistoryDisplayEntry[]>([]);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState('');

  // Form states for Add/Edit
  const [formUsername, setFormUsername] = useState('');
  const [formPhone, setFormPhone] = useState('');
  const [formChannels, setFormChannels] = useState<string[]>([]);
  const [formPoints, setFormPoints] = useState(2000);
  const [formRole, setFormRole] = useState<'admin' | 'user'>('user');
  const [formStatus, setFormStatus] = useState<'active' | 'disabled'>('active');
  const [isSaving, setIsSaving] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  const loadUsers = useCallback(async (keyword: string, channel: string) => {
    setIsListLoading(true);
    setListError('');
    try {
      const result = await listAdminUsers({
        keyword: keyword || undefined,
        channel: channel || undefined,
        limit: 100,
      });
      setUsers(result.items);
    } catch (err) {
      setListError(getErrorMessage(err, '加载账号列表失败'));
    } finally {
      setIsListLoading(false);
    }
  }, []);

  // 打开面板 / 搜索词（300ms 防抖）/ 渠道筛选变化时重新拉取列表
  useEffect(() => {
    if (!isOpen) return;
    const timer = setTimeout(() => {
      void loadUsers(searchQuery.trim(), selectedFilterChannel);
    }, 300);
    return () => clearTimeout(timer);
  }, [isOpen, searchQuery, selectedFilterChannel, loadUsers]);

  const handleOpenAdd = () => {
    setFormUsername('');
    setFormPhone('');
    setFormChannels(['xiaohongshu', 'douyin']);
    setFormPoints(2000);
    setFormRole('user');
    setFormStatus('active');
    setErrorMsg('');
    setSuccessMsg('');
    setIsAdding(true);
    setEditingAccount(null);
    setSelectedHistoryUser(null);
  };

  const handleOpenEdit = (acc: ApiAdminUser) => {
    setEditingAccount(acc);
    setFormUsername(acc.nickname);
    setFormPhone(acc.phone ?? '');
    setFormChannels(acc.channels);
    setFormPoints(acc.points);
    setFormRole(acc.role);
    setFormStatus(acc.status);
    setErrorMsg('');
    setSuccessMsg('');
    setIsAdding(false);
    setSelectedHistoryUser(null);
  };

  const handleOpenHistory = async (acc: ApiAdminUser) => {
    setSelectedHistoryUser(acc);
    setHistoryFilterType('day');
    setIsAdding(false);
    setEditingAccount(null);
    setIsHistoryLoading(true);
    setHistoryError('');
    setHistoryEntries([]);
    try {
      const result = await getAdminUserPointsHistory(acc.id, { limit: 200 });
      setHistoryEntries(result.items.map(mapHistoryEntry));
    } catch (err) {
      setHistoryError(getErrorMessage(err, '加载积分流水失败'));
    } finally {
      setIsHistoryLoading(false);
    }
  };

  const handleToggleChannel = (channel: string) => {
    setFormChannels(prev =>
      prev.includes(channel)
        ? prev.filter(c => c !== channel)
        : [...prev, channel]
    );
  };

  const handleSave = async (e: React.FormEvent) => {
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

    setIsSaving(true);
    try {
      if (isAdding) {
        await createAdminUser({
          nickname: formUsername.trim(),
          phone: formPhone,
          role: formRole,
          points: formPoints,
          channels: formChannels,
        });
        setSuccessMsg(`账号「${formUsername.trim()}」创建成功！`);
      } else if (editingAccount) {
        await updateAdminUser(editingAccount.id, {
          nickname: formUsername.trim(),
          phone: formPhone,
          role: formRole,
          channels: formChannels,
          status: formStatus,
        });
        if (formPoints !== editingAccount.points) {
          await adjustAdminUserPoints(
            editingAccount.id,
            formPoints - editingAccount.points,
            '管理后台积分调整',
            crypto.randomUUID(),
          );
        }
        setSuccessMsg(`账号「${formUsername.trim()}」修改成功！`);
      }

      await loadUsers(searchQuery.trim(), selectedFilterChannel);
      setTimeout(() => {
        setIsAdding(false);
        setEditingAccount(null);
        setSuccessMsg('');
      }, 1500);
    } catch (err) {
      setErrorMsg(getErrorMessage(err, '保存失败，请稍后重试'));
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`确认要删除账号「${name}」吗？`)) return;
    try {
      await deleteAdminUser(id);
      await loadUsers(searchQuery.trim(), selectedFilterChannel);
    } catch (err) {
      setListError(getErrorMessage(err, '删除账号失败'));
    }
  };

  if (!isOpen) return null;

  const settleEntries = historyEntries.filter(entry => entry.kind === 'settle');
  const totalConsumed = settleEntries.reduce((sum, item) => sum + item.points, 0);
  const chartData = getChartData(settleEntries, historyFilterType);

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
                      {selectedFilterChannel === ''
                        ? '所有渠道'
                        : channelLabel(selectedFilterChannel)
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
                        <div className="px-2 py-1 border-b border-slate-100 text-[9px] font-bold text-slate-400">
                          渠道筛选（单选）
                        </div>
                        {[{ slug: '', label: '所有渠道' }, ...CHANNEL_OPTIONS].map(ch => {
                          const isSelected = selectedFilterChannel === ch.slug;
                          return (
                            <button
                              key={ch.slug || 'all'}
                              type="button"
                              onClick={() => {
                                setSelectedFilterChannel(ch.slug);
                                setIsFilterDropdownOpen(false);
                              }}
                              className={`w-full py-1 px-2 rounded-md text-left font-medium transition flex items-center justify-between hover:bg-slate-50 ${
                                isSelected ? 'bg-indigo-50/50 text-indigo-600 font-semibold' : 'text-slate-600'
                              }`}
                            >
                              <span>{ch.label}</span>
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
                  onClick={handleOpenAdd}
                  className="px-3.5 py-1.5 text-xs font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-sm transition flex items-center gap-1.5 active:scale-95"
                >
                  <UserPlus className="h-3.5 w-3.5" />
                  <span>新增账号</span>
                </button>
              </div>
            </div>

            {listError && (
              <div className="flex items-center gap-1.5 bg-rose-50 border border-rose-100 p-2.5 rounded-lg text-rose-600 text-xs font-medium">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                <span>{listError}</span>
              </div>
            )}

            {/* Account List Grid */}
            <div className="flex-1 overflow-y-auto border border-slate-100 rounded-xl bg-slate-50/20 max-h-[50vh] md:max-h-none">
              {isListLoading ? (
                <div className="flex flex-col items-center justify-center py-16 text-slate-400">
                  <Users className="h-10 w-10 text-slate-300 mb-2 stroke-[1.5] animate-pulse" />
                  <p className="text-xs">账号列表加载中...</p>
                </div>
              ) : users.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 text-slate-400">
                  <Users className="h-10 w-10 text-slate-300 mb-2 stroke-[1.5]" />
                  <p className="text-xs">未找到符合条件的账号记录</p>
                </div>
              ) : (
                <div className="divide-y divide-slate-100">
                  {users.map(acc => {
                    const isSelf = acc.id === currentUserId;
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
                            <span className="font-bold text-xs text-slate-800">{acc.nickname}</span>
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
                            {acc.status === 'disabled' && (
                              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-rose-50 text-rose-600 border border-rose-200/50">
                                已禁用
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
                              {acc.phone ?? '未绑定手机'}
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
                                {channelLabel(ch)}
                              </span>
                            ))}
                          </div>
                        </div>

                        {/* Actions */}
                        <div className="flex items-center gap-1.5 self-end sm:self-center">
                          <button
                            onClick={() => void handleOpenHistory(acc)}
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
                            onClick={() => void handleDelete(acc.id, acc.nickname)}
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

              <form onSubmit={e => void handleSave(e)} className="space-y-4 text-xs">
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
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">
                    {isAdding ? '初始积分余额' : '调整积分余额'}
                  </label>
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

                {/* Status (edit only, hidden for self) */}
                {!isAdding && editingAccount && editingAccount.id !== currentUserId && (
                  <div className="space-y-1">
                    <label className="block text-[10px] font-bold text-slate-400 uppercase">账号状态</label>
                    <div className="flex gap-2">
                      <label className="flex-1 flex items-center gap-1.5 p-2 bg-white border border-slate-200 rounded-lg cursor-pointer hover:bg-indigo-50/10 transition">
                        <input
                          type="radio"
                          name="formStatus"
                          checked={formStatus === 'active'}
                          onChange={() => setFormStatus('active')}
                          className="text-indigo-600 focus:ring-indigo-500 h-3.5 w-3.5"
                        />
                        <span>正常</span>
                      </label>
                      <label className="flex-1 flex items-center gap-1.5 p-2 bg-white border border-slate-200 rounded-lg cursor-pointer hover:bg-rose-50/30 transition">
                        <input
                          type="radio"
                          name="formStatus"
                          checked={formStatus === 'disabled'}
                          onChange={() => setFormStatus('disabled')}
                          className="text-rose-600 focus:ring-rose-500 h-3.5 w-3.5"
                        />
                        <span>禁用</span>
                      </label>
                    </div>
                  </div>
                )}

                {/* Channel Permissions checkboxes */}
                <div className="space-y-1.5">
                  <label className="block text-[10px] font-bold text-slate-400 uppercase">渠道分析权限</label>
                  <div className="grid grid-cols-2 gap-1.5">
                    {CHANNEL_OPTIONS.map(ch => {
                      const isSelected = formChannels.includes(ch.slug);
                      return (
                        <button
                          key={ch.slug}
                          type="button"
                          onClick={() => handleToggleChannel(ch.slug)}
                          className={`py-1.5 px-2 rounded-lg border text-left font-medium transition flex items-center justify-between ${
                            isSelected
                              ? 'bg-indigo-50 border-indigo-200 text-indigo-700 font-semibold'
                              : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
                          }`}
                        >
                          <span>{ch.label}</span>
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
                    disabled={isSaving}
                    className={`flex-1 py-2 text-white font-bold rounded-lg shadow-sm transition text-center ${
                      isSaving ? 'bg-indigo-300 cursor-not-allowed' : 'bg-indigo-600 hover:bg-indigo-700'
                    }`}
                  >
                    {isSaving ? '保存中...' : isAdding ? '立即添加' : '保存修改'}
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
                    「{selectedHistoryUser.nickname}」积分消耗分析
                  </span>
                </div>
                <button
                  onClick={() => setSelectedHistoryUser(null)}
                  className="text-slate-400 hover:text-slate-600 text-xs font-bold"
                >
                  关闭
                </button>
              </div>

              {isHistoryLoading ? (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-400 py-16">
                  <History className="h-8 w-8 text-slate-300 stroke-[1.5] mb-1.5 animate-pulse" />
                  <p className="text-[10.5px]">积分流水加载中...</p>
                </div>
              ) : historyError ? (
                <div className="flex items-center gap-1.5 bg-rose-50 border border-rose-100 p-2.5 rounded-lg text-rose-600 text-xs font-medium">
                  <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  <span>{historyError}</span>
                </div>
              ) : (
                <>
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
                        <span>总消耗: {totalConsumed.toLocaleString()} 积分</span>
                      </div>
                    </div>

                    <div className="h-44 w-full text-xs">
                      {settleEntries.length === 0 ? (
                        <div className="h-full flex flex-col items-center justify-center text-slate-400">
                          <BarChart3 className="h-8 w-8 text-slate-300 stroke-[1.5] mb-1.5" />
                          <p className="text-[10.5px]">暂无历史消耗数据</p>
                        </div>
                      ) : (
                        <ResponsiveContainer width="100%" height="100%">
                          <BarChart
                            data={chartData}
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
                              {chartData.map((entry, idx) => (
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
                      积分流水明细
                    </span>
                    <div className="flex-1 overflow-y-auto border border-slate-100 rounded-xl bg-white divide-y divide-slate-50 p-1">
                      {historyEntries.length === 0 ? (
                        <div className="p-6 text-center text-[10.5px] text-slate-400">暂无积分流水记录</div>
                      ) : historyEntries.map(entry => {
                        const badge = KIND_BADGES[entry.kind] ?? {
                          label: entry.kind,
                          className: 'bg-slate-100 text-slate-500 border-slate-200',
                        };
                        const amountText = entry.kind === 'settle'
                          ? `-${entry.points}`
                          : `${entry.points >= 0 ? '+' : ''}${entry.points}`;
                        return (
                          <div key={entry.id} className="p-2.5 flex items-center justify-between text-xs hover:bg-slate-50 rounded-lg transition duration-150">
                            <div className="space-y-1 min-w-0 pr-2">
                              <p className="font-bold text-slate-700 truncate text-[11px]" title={entry.title}>
                                {entry.title}
                              </p>
                              <div className="flex items-center gap-2 text-[10px] text-slate-400 font-medium">
                                <span className={`px-1 py-0.2 rounded border shrink-0 ${badge.className}`}>
                                  {badge.label}
                                </span>
                                {entry.platformLabel && (
                                  <span className="flex items-center gap-0.5 bg-indigo-50/50 text-indigo-600 px-1 py-0.2 rounded border border-indigo-100/20 shrink-0">
                                    {entry.platformLabel}
                                  </span>
                                )}
                                <span className="flex items-center gap-0.5">
                                  <Calendar className="h-2.5 w-2.5" />
                                  {entry.date}
                                </span>
                              </div>
                            </div>
                            <div className="text-right shrink-0">
                              <span className={`text-[11px] font-extrabold px-1.5 py-0.5 rounded font-mono border ${
                                entry.kind === 'settle'
                                  ? 'text-rose-600 bg-rose-50 border-rose-100'
                                  : entry.points >= 0
                                    ? 'text-emerald-600 bg-emerald-50 border-emerald-100'
                                    : 'text-amber-600 bg-amber-50 border-amber-100'
                              }`}>
                                {amountText}
                              </span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </>
              )}
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
