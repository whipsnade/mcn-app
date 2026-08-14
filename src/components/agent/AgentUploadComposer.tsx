import { FileUp, X } from 'lucide-react';

import type { ApiAgentUpload } from '../../api/agent';

export interface AgentUploadComposerProps {
  uploads: ApiAgentUpload[];
  disabled?: boolean;
  onUpload: (file: File) => void;
  onRemove: (uploadId: string) => void;
}

/** 资料选择仅支持后端契约允许的 CSV/XLSX；移除只影响本次发送选择，不删除审计记录。 */
export default function AgentUploadComposer({ uploads, disabled, onUpload, onRemove }: AgentUploadComposerProps) {
  return (
    <section aria-label="上传资料" className="flex flex-wrap items-center gap-2">
      <label className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600">
        <FileUp className="h-3 w-3" aria-hidden="true" /> 上传资料
        <input
          aria-label="上传资料"
          className="sr-only"
          type="file"
          accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          disabled={disabled}
          onChange={event => {
            const file = event.currentTarget.files?.[0];
            event.currentTarget.value = '';
            const extension = file?.name.split('.').pop()?.toLowerCase();
            if (file && (extension === 'csv' || extension === 'xlsx')) onUpload(file);
          }}
        />
      </label>
      {uploads.map(upload => (
        <span key={upload.id} className="inline-flex items-center gap-1 rounded-lg bg-slate-100 px-2 py-1 text-[10px] text-slate-600">
          {upload.original_filename} · {upload.status === 'parsed' ? '已解析' : upload.status === 'failed' ? '失败' : '上传中'}
          <button type="button" aria-label={`移除${upload.original_filename}`} onClick={() => onRemove(upload.id)}>
            <X className="h-3 w-3" aria-hidden="true" />
          </button>
        </span>
      ))}
    </section>
  );
}
