import React, { useState } from 'react';
import { Check, Sparkles, X } from 'lucide-react';


export interface NewSessionData {
  brand: string;
  campaignName: string;
  platforms: string[];
  category: string;
  kolName: string;
  targetAudience: string;
  budgetMin?: string;
  budgetMax?: string;
  initialQuery: string;
}

interface NewSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreate: (data: NewSessionData) => void | Promise<void>;
}

const PLATFORMS = [
  { value: 'xiaohongshu', label: '小红书' },
  { value: 'douyin', label: '抖音' },
  { value: 'bilibili', label: '哔哩哔哩' },
  { value: 'weibo', label: '微博' },
  { value: 'wechat', label: '微信' },
];


export default function NewSessionModal({ isOpen, onClose, onCreate }: NewSessionModalProps) {
  const [brand, setBrand] = useState('');
  const [campaignName, setCampaignName] = useState('');
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>([]);
  const [category, setCategory] = useState('');
  const [kolName, setKolName] = useState('');
  const [targetAudience, setTargetAudience] = useState('');
  const [budgetMin, setBudgetMin] = useState('');
  const [budgetMax, setBudgetMax] = useState('');
  const [initialQuery, setInitialQuery] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (!isOpen) return null;

  const reset = () => {
    setBrand('');
    setCampaignName('');
    setSelectedPlatforms([]);
    setCategory('');
    setKolName('');
    setTargetAudience('');
    setBudgetMin('');
    setBudgetMax('');
    setInitialQuery('');
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!category.trim() || !initialQuery.trim()) {
      return;
    }

    const trimmedBrand = brand.trim();
    const trimmedCampaignName = campaignName.trim();
    setIsSubmitting(true);
    try {
      await onCreate({
        brand: trimmedBrand,
        campaignName: trimmedCampaignName,
        platforms: selectedPlatforms,
        category: category.trim(),
        kolName: kolName.trim(),
        targetAudience: targetAudience.trim(),
        budgetMin: budgetMin || undefined,
        budgetMax: budgetMax || undefined,
        initialQuery: initialQuery.trim(),
      });
      reset();
      onClose();
    } catch {
      // The workspace hook exposes the request error in the main shell.
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity"
        onClick={onClose}
      />

      <div className="relative w-full max-w-lg overflow-hidden rounded-2xl bg-white shadow-2xl border border-slate-100 animate-in fade-in zoom-in-95 duration-200">
        <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-slate-800 font-display">新建 KOL 智能筛选会话</h3>
              <p className="text-xs text-slate-400">设定筛选条件，由 AI 生成候选清单与 BI 报告</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1 text-slate-400 hover:bg-slate-50 hover:text-slate-600 transition"
            aria-label="关闭"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4 max-h-[75vh] overflow-y-auto">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">品牌名称</label>
              <input
                placeholder="例如：雅诗兰黛"
                value={brand}
                onChange={event => setBrand(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">活动/项目名称</label>
              <input
                placeholder="例如：双11抗老宣发"
                value={campaignName}
                onChange={event => setCampaignName(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">筛选渠道（可多选）</label>
            <div className="flex flex-wrap gap-1.5">
              {PLATFORMS.map(option => {
                const isSelected = selectedPlatforms.includes(option.value);
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      if (isSelected) {
                        setSelectedPlatforms(selectedPlatforms.filter(platform => platform !== option.value));
                      } else if (!isSelected) {
                        setSelectedPlatforms([...selectedPlatforms, option.value]);
                      }
                    }}
                    className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-medium border transition-all duration-150 active:scale-95 ${
                      isSelected
                        ? 'bg-indigo-50 border-indigo-200 text-indigo-700 font-semibold shadow-sm'
                        : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    {isSelected
                      ? <Check className="h-3 w-3 text-indigo-600 shrink-0" />
                      : <span className="w-3 h-3 rounded border border-slate-300 shrink-0" />}
                    <span>{option.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="session-industry" className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">行业筛选 *</label>
              <select
                id="session-industry"
                required
                value={category}
                onChange={event => setCategory(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              >
                <option value="" disabled>请选择行业</option>
                {['餐饮', '茶饮', '美妆', '护肤'].map(industry => <option key={industry} value={industry}>{industry}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">目标人群</label>
              <input
                placeholder="例如：25-35 岁一线城市女性"
                value={targetAudience}
                onChange={event => setTargetAudience(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">KOL 名称搜索</label>
            <input placeholder="例如：李佳琦" value={kolName} onChange={event => setKolName(event.target.value)} className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition" />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">最低预算（元）</label>
              <input
                type="number"
                min="0"
                step="0.01"
                placeholder="例如：10000"
                value={budgetMin}
                onChange={event => setBudgetMin(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">最高预算（元）</label>
              <input
                type="number"
                min="0"
                step="0.01"
                placeholder="例如：50000"
                value={budgetMax}
                onChange={event => setBudgetMax(event.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">初始分析指令 / 提问 *</label>
            <textarea
              required
              placeholder="例如：筛选 20 位近 30 天互动稳定、女性粉丝占比高的达人，并按预算匹配度排序。"
              value={initialQuery}
              onChange={event => setInitialQuery(event.target.value)}
              rows={3}
              className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
            />
          </div>

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 rounded-xl border border-slate-200 py-2.5 text-sm font-medium text-slate-600 hover:bg-slate-50 active:bg-slate-100 transition"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="flex-1 flex items-center justify-center gap-1.5 rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white shadow-md hover:bg-indigo-700 active:scale-[0.98] transition disabled:opacity-60"
            >
              <Check className="h-4 w-4" />
              {isSubmitting ? '正在创建…' : '立即创建'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
