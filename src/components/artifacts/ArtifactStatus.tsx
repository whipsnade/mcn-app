import { ShieldAlert } from 'lucide-react';

/** 产物状态展示基元：draft/reviewing/published 状态 chip + restricted 受限徽标。
 *
 * ``status`` 是 Artifact 生命周期（generating/restricted/reviewing/published，
 * §12.3 Draft 展示必须标识）；``dataStatus`` 是 payload.data_status
 * （complete/restricted，§12.1）。restricted 产物同时展示受限徽标。
 */

const STATUS_META: Record<string, { label: string; className: string }> = {
  generating: { label: '生成中', className: 'bg-indigo-50 text-indigo-600' },
  draft: { label: '草稿', className: 'bg-slate-100 text-slate-500' },
  restricted: { label: '受限', className: 'bg-amber-50 text-amber-600' },
  reviewing: { label: '审核中', className: 'bg-amber-50 text-amber-600' },
  published: { label: '已发布', className: 'bg-emerald-50 text-emerald-600' },
  failed: { label: '生成失败', className: 'bg-rose-50 text-rose-600' },
};

export interface ArtifactStatusProps {
  status?: string;
  dataStatus?: string;
}

export function ArtifactStatus({ status, dataStatus }: ArtifactStatusProps) {
  const meta = status ? STATUS_META[status] : undefined;
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      {meta && (
        <span className={`rounded-full px-2 py-0.5 text-[9px] font-semibold ${meta.className}`}>
          {meta.label}
        </span>
      )}
      {dataStatus === 'restricted' && (
        <span className="flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[9px] font-semibold text-amber-600">
          <ShieldAlert className="h-3 w-3" aria-hidden="true" />
          数据受限
        </span>
      )}
    </span>
  );
}
