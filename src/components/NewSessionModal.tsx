import React, { useState } from 'react';
import { X, Plus, Sparkles, Check } from 'lucide-react';

interface NewSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreate: (data: {
    brand: string;
    campaignName: string;
    platform: string;
    mcn: string;
    kols: string[];
    initialQuery: string;
  }) => void;
}

const PLATFORMS = [
  { value: 'Xiaohongshu', label: '小红书 (Xiaohongshu)' },
  { value: 'Douyin', label: '抖音 (Douyin)' },
  { value: 'Bilibili', label: '哔哩哔哩 (Bilibili)' },
  { value: 'Weibo', label: '微博 (Weibo)' },
  { value: 'YouTube', label: 'YouTube' },
  { value: 'Instagram', label: 'Instagram' }
];

export default function NewSessionModal({ isOpen, onClose, onCreate }: NewSessionModalProps) {
  const [brand, setBrand] = useState('');
  const [campaignName, setCampaignName] = useState('');
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>(['Xiaohongshu']);
  const [mcn, setMcn] = useState('');
  const [kolInput, setKolInput] = useState('');
  const [initialQuery, setInitialQuery] = useState('');

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!brand.trim() || !campaignName.trim()) return;

    const kols = kolInput
      .split(/[,，\n]/)
      .map(k => k.trim())
      .filter(k => k.length > 0);

    onCreate({
      brand: brand.trim(),
      campaignName: campaignName.trim(),
      platform: selectedPlatforms.join(','),
      mcn: mcn.trim() || '自主直连 (Direct)',
      kols: kols.length > 0 ? kols : ['默认种草达人A', '默认种草达人B'],
      initialQuery: initialQuery.trim() || `开始分析「${brand}」品牌旗下「${campaignName}」的红人及MCN营销效果。`
    });

    // Reset fields
    setBrand('');
    setCampaignName('');
    setSelectedPlatforms(['Xiaohongshu']);
    setMcn('');
    setKolInput('');
    setInitialQuery('');
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div 
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity"
        onClick={onClose}
      />

      {/* Content Container */}
      <div className="relative w-full max-w-lg overflow-hidden rounded-2xl bg-white shadow-2xl border border-slate-100 animate-in fade-in zoom-in-95 duration-200">
        
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-slate-800 font-display">新建 KOL/MCN 效果会话</h3>
              <p className="text-xs text-slate-400">设定营销参数，由 AI 自动生成全套数据报表</p>
            </div>
          </div>
          <button 
            onClick={onClose}
            className="rounded-lg p-1 text-slate-400 hover:bg-slate-50 hover:text-slate-600 transition"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Form Body */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4 max-h-[75vh] overflow-y-auto">
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">
                品牌名称 *
              </label>
              <input
                type="text"
                required
                placeholder="例如：雅诗兰黛"
                value={brand}
                onChange={e => setBrand(e.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">
                活动/项目名称 *
              </label>
              <input
                type="text"
                required
                placeholder="例如：双11抗老宣发"
                value={campaignName}
                onChange={e => setCampaignName(e.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">
                投放平台 (可多选) *
              </label>
              <div className="flex flex-wrap gap-1.5 max-h-[140px] overflow-y-auto pr-1">
                {PLATFORMS.map(opt => {
                  const isSelected = selectedPlatforms.includes(opt.value);
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => {
                        if (isSelected) {
                          if (selectedPlatforms.length > 1) {
                            setSelectedPlatforms(selectedPlatforms.filter(p => p !== opt.value));
                          }
                        } else {
                          setSelectedPlatforms([...selectedPlatforms, opt.value]);
                        }
                      }}
                      className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-medium border transition-all duration-150 active:scale-95 ${
                        isSelected
                          ? 'bg-indigo-50 border-indigo-200 text-indigo-700 font-semibold shadow-sm'
                          : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
                      }`}
                    >
                      {isSelected ? (
                        <Check className="h-3 w-3 text-indigo-600 shrink-0" />
                      ) : (
                        <span className="w-3 h-3 rounded border border-slate-300 shrink-0" />
                      )}
                      <span>{opt.label.split(' ')[0]}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">
                合作 MCN 机构
              </label>
              <input
                type="text"
                placeholder="例如：无忧传媒"
                value={mcn}
                onChange={e => setMcn(e.target.value)}
                className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">
              合作红人 KOL 列表 (每行或逗号分隔)
            </label>
            <textarea
              placeholder="例如：李佳琦, 崔佳楠, 骆王宇&#10;或者每行输入一个"
              value={kolInput}
              onChange={e => setKolInput(e.target.value)}
              rows={2}
              className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-1">
              初始分析指令 / 提问
            </label>
            <textarea
              placeholder="例如：帮我重点分析这次抗衰系列中，针对轻熟女受众的情感偏好以及高热词，并评估MCN的投流ROI。"
              value={initialQuery}
              onChange={e => setInitialQuery(e.target.value)}
              rows={3}
              className="w-full rounded-xl border border-slate-200 px-3.5 py-2 text-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none transition"
            />
          </div>

          {/* Action Buttons */}
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
              className="flex-1 flex items-center justify-center gap-1.5 rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white shadow-md hover:bg-indigo-700 active:scale-[0.98] transition"
            >
              <Check className="h-4 w-4" />
              立即创建
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
