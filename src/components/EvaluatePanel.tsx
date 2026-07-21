import { FileSpreadsheet, Upload, X } from 'lucide-react';
import { useState } from 'react';

import type { ApiQuickEvaluateResult } from '../api/contracts';
import { postEvaluate, quickErrorMessage } from '../api/quick';
import { MarkdownBlock } from './UniversalReport';

const MAX_FILE_BYTES = 5 * 1024 * 1024;

function validateFile(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!name.endsWith('.xlsx') && !name.endsWith('.csv')) return '仅支持 xlsx 或 csv 文件';
  if (file.size > MAX_FILE_BYTES) return '文件不能超过 5MB';
  return null;
}

export default function EvaluatePanel() {
  const [isModalOpen, setIsModalOpen] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ApiQuickEvaluateResult | null>(null);
  const [error, setError] = useState<string>();

  const handleSelectFile = (next: File | null) => {
    setFile(next);
    setFileError(next ? validateFile(next) ?? undefined : undefined);
  };

  const handleSubmit = async () => {
    if (!file || submitting) return;
    const invalid = validateFile(file);
    if (invalid) {
      setFileError(invalid);
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      const evaluated = await postEvaluate(file);
      setResult(evaluated);
      setIsModalOpen(false);
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
          正在分析上传数据…
        </div>
      ) : result ? (
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-4">
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-2 px-1">
              <h3 className="text-[13px] font-bold text-slate-800">{result.title || '评估结果'}</h3>
              <button
                type="button"
                onClick={() => setIsModalOpen(true)}
                className="shrink-0 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-bold text-slate-500 transition hover:bg-slate-50 active:scale-95"
              >
                重新上传
              </button>
            </div>
            {error && (
              <p role="alert" className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-[11px] font-medium text-rose-600">{error}</p>
            )}
            <MarkdownBlock block={{ type: 'markdown', text: result.analysis_markdown }} />
          </div>
        </div>
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 bg-slate-50 text-xs font-medium text-slate-400">
          {error && <span role="alert" className="text-rose-500">{error}</span>}
          <p>上传 xlsx / csv 数据表格（≤5MB），生成社媒热度分析</p>
          <button
            type="button"
            onClick={() => setIsModalOpen(true)}
            className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700 active:scale-95"
          >
            <Upload className="h-3.5 w-3.5" />
            选择文件
          </button>
        </div>
      )}

      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 backdrop-blur-xs font-sans">
          <div className="relative w-full max-w-md rounded-2xl border border-slate-100 bg-white p-6 shadow-2xl">
            <button
              type="button"
              onClick={() => setIsModalOpen(false)}
              aria-label="关闭"
              className="absolute right-4 top-4 rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-50 hover:text-slate-600"
            >
              <X className="h-4 w-4" />
            </button>
            <div className="flex items-center gap-2">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600">
                <FileSpreadsheet className="h-5 w-5" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-800">上传数据表格</h3>
                <p className="text-[10px] text-slate-400">支持 xlsx / csv，单个文件不超过 5MB</p>
              </div>
            </div>

            <input
              type="file"
              accept=".xlsx,.csv"
              aria-label="选择数据表格"
              onChange={event => handleSelectFile(event.target.files?.[0] ?? null)}
              className="mt-4 w-full rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-indigo-50 file:px-2.5 file:py-1 file:text-[11px] file:font-semibold file:text-indigo-600"
            />
            {fileError && (
              <p role="alert" className="mt-2 text-[11px] font-medium text-rose-600">{fileError}</p>
            )}
            {error && !result && (
              <p role="alert" className="mt-2 text-[11px] font-medium text-rose-600">{error}</p>
            )}

            <button
              type="button"
              disabled={!file || Boolean(fileError) || submitting}
              onClick={() => void handleSubmit()}
              className="mt-4 w-full rounded-xl bg-indigo-600 py-2.5 text-xs font-bold text-white shadow-md transition hover:bg-indigo-700 active:scale-[0.98] disabled:cursor-not-allowed disabled:bg-indigo-300"
            >
              {submitting ? '分析中…' : '开始评估'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
