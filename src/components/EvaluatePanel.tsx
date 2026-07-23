import { ClipboardList, X } from 'lucide-react';
import { useState } from 'react';

import type { ApiQuickEvaluateResult } from '../api/contracts';
import { postEvaluate, quickErrorMessage } from '../api/quick';
import { MarkdownBlock } from './UniversalReport';

const MAX_KOL_NAMES = 20;

export default function EvaluatePanel() {
  const [activityName, setActivityName] = useState('');
  const [kolNames, setKolNames] = useState<string[]>([]);
  const [kolDraft, setKolDraft] = useState('');
  const [kolHint, setKolHint] = useState<string>();
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ApiQuickEvaluateResult | null>(null);
  const [error, setError] = useState<string>();

  // 回车/逗号/失焦都会触发添加；支持逗号分隔批量粘贴，strip 后去重。
  const addKolNames = (raw: string) => {
    const parts = raw.split(/[,，]/).map(part => part.trim()).filter(Boolean);
    if (parts.length === 0) {
      setKolDraft('');
      return;
    }
    let overflow = false;
    const next = [...kolNames];
    for (const part of parts) {
      if (next.includes(part)) continue;
      if (next.length >= MAX_KOL_NAMES) {
        overflow = true;
        break;
      }
      next.push(part);
    }
    setKolHint(overflow ? `最多添加 ${MAX_KOL_NAMES} 位达人` : undefined);
    setKolNames(next);
    setKolDraft('');
  };

  const removeKolName = (name: string) => {
    setKolNames(kolNames.filter(item => item !== name));
    setKolHint(undefined);
  };

  const handleKolDraftChange = (value: string) => {
    if (value.includes(',') || value.includes('，')) {
      addKolNames(value);
    } else {
      setKolDraft(value);
    }
  };

  const canSubmit = activityName.trim().length > 0 && kolNames.length > 0 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(undefined);
    try {
      const evaluated = await postEvaluate({ activityName: activityName.trim(), kolNames });
      setResult(evaluated);
    } catch (err) {
      setError(quickErrorMessage(err, '评估失败，请稍后重试'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-slate-50">
      <div className="flex h-12 shrink-0 items-center border-b border-slate-200 bg-white px-4">
        <h2 className="text-xs font-bold text-slate-800">达人/活动评估</h2>
      </div>

      {submitting && !result ? (
        <div className="flex flex-1 items-center justify-center bg-slate-50 text-xs font-medium text-slate-400">
          评估中，可能需要几分钟…
        </div>
      ) : result ? (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-2 px-1">
              <h3 className="text-[13px] font-bold text-slate-800">{result.title || '评估结果'}</h3>
              <button
                type="button"
                onClick={() => setResult(null)}
                className="shrink-0 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-bold text-slate-500 transition hover:bg-slate-50 active:scale-95"
              >
                重新评估
              </button>
            </div>
            {error && (
              <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>
            )}
            <MarkdownBlock block={{ type: 'markdown', text: result.analysis_markdown }} />
          </div>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="mx-auto max-w-lg space-y-4">
            <div className="flex items-center gap-2">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600">
                <ClipboardList className="h-5 w-5" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-800">输入活动与达人名单</h3>
                <p className="text-[10px] text-slate-400">AI 将逐个达人查证数据，评估与活动的匹配度</p>
              </div>
            </div>

            <div>
              <label htmlFor="evaluate-activity-name" className="text-[11px] font-bold text-slate-600">活动名称</label>
              <input
                id="evaluate-activity-name"
                type="text"
                value={activityName}
                maxLength={100}
                onChange={event => setActivityName(event.target.value)}
                placeholder="例如：火锅节新品推广"
                className="mt-1.5 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-indigo-300 focus:ring-2 focus:ring-indigo-500/20"
              />
            </div>

            <div>
              <label htmlFor="evaluate-kol-name" className="text-[11px] font-bold text-slate-600">达人名称</label>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2 py-1.5 transition focus-within:border-indigo-300 focus-within:ring-2 focus-within:ring-indigo-500/20">
                {kolNames.map(name => (
                  <span
                    key={name}
                    className="flex items-center gap-1 rounded-md border border-indigo-100 bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-700"
                  >
                    {name}
                    <button
                      type="button"
                      aria-label={`移除 ${name}`}
                      onClick={() => removeKolName(name)}
                      className="rounded-sm text-indigo-400 transition hover:text-indigo-600"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                <input
                  id="evaluate-kol-name"
                  type="text"
                  value={kolDraft}
                  onChange={event => handleKolDraftChange(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      addKolNames(kolDraft);
                    }
                  }}
                  onBlur={() => addKolNames(kolDraft)}
                  placeholder={kolNames.length === 0 ? '输入达人名称，回车或逗号添加' : ''}
                  className="min-w-[120px] flex-1 border-none bg-transparent px-1 py-1 text-xs text-slate-700 outline-none placeholder:text-slate-400"
                />
              </div>
              <div className="mt-1 flex items-center justify-between text-[10px] text-slate-400">
                <span>回车 / 逗号 / 失焦添加，点击 × 移除</span>
                <span>{kolNames.length}/{MAX_KOL_NAMES}</span>
              </div>
              {kolHint && (
                <p role="alert" className="mt-1 text-[11px] font-medium text-rose-600">{kolHint}</p>
              )}
            </div>

            {error && (
              <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>
            )}

            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => void handleSubmit()}
              className="w-full rounded-xl bg-indigo-600 py-2.5 text-xs font-bold text-white shadow-md transition hover:bg-indigo-700 active:scale-[0.98] disabled:cursor-not-allowed disabled:bg-indigo-300"
            >
              开始评估
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
