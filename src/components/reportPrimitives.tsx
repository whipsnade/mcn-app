import { HelpCircle } from 'lucide-react';
import type { ReactNode } from 'react';

// 报告类面板共用的展示基元，供 UniversalReport 复用。

/** 指标卡数据形状；由 UniversalReport 按报告块数据内联构造。 */
export interface PrimitiveMetric {
  value: number | null;
  unit: string;
  available: boolean;
  coverage: number;
  source_fields: string[];
  platforms: string[];
}

export function Card({
  title,
  icon,
  hint,
  badge,
  children,
  className = '',
}: {
  title: string;
  icon: ReactNode;
  hint?: string;
  /** 标题旁的标记（如章节受限徽标）。 */
  badge?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-xl border border-slate-100 bg-white p-3.5 shadow-sm ${className}`}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="flex min-w-0 items-center gap-1.5 text-[14px] font-bold text-slate-700">
          <span className="shrink-0 text-indigo-500">{icon}</span>
          <span className="truncate">{title}</span>
          {badge}
          <HelpCircle aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-slate-300" />
        </h3>
        {hint && <span className="shrink-0 text-[10px] text-slate-400">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

export function Missing({ label = '数据不足' }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[42px] items-center justify-center rounded-lg bg-slate-50 px-2 text-[11px] text-slate-400">
      {label}
    </div>
  );
}

export function metricAvailable(metric: PrimitiveMetric | undefined): metric is PrimitiveMetric {
  return Boolean(metric?.available && metric.value !== null && Number.isFinite(metric.value));
}

export function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

export function formatExposure(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}M`;
  if (value >= 10_000) return `${(value / 10_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}万`;
  return formatNumber(value);
}

export function MetricCard({ label, metric, format = 'number', accent = 'text-slate-800' }: { label: string; metric?: PrimitiveMetric; format?: 'number' | 'exposure' | 'percent'; accent?: string }) {
  const value = metricAvailable(metric)
    ? format === 'percent'
      ? `${metric.value}%`
      : format === 'exposure'
        ? formatExposure(metric.value)
        : formatNumber(metric.value)
    : '数据不足';
  return (
    <section className="min-w-0 rounded-xl border border-slate-100 bg-white px-3.5 py-3.5 shadow-sm">
      <div className="flex items-center gap-1 text-[12px] font-medium text-slate-400">
        <span className="truncate">{label}</span>
        <HelpCircle aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-slate-300" />
      </div>
      <p className={`mt-2 truncate text-[24px] font-bold leading-none tracking-tight ${metricAvailable(metric) ? accent : 'text-slate-300'}`}>
        {value}
      </p>
      {metricAvailable(metric) && <p className="mt-1 text-[10px] text-slate-400">{metric.unit}</p>}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Artifact 视图共用：null 业务数值统一展示「数据受限」，绝不把 null 当 0。
// （§12.1：业务数值允许 null，但对应路径必须处于 partial/unavailable，
// 前端显示「数据受限」。）
// --------------------------------------------------------------------------- //

export function restrictedCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatExposure(value) : '数据受限';
}

export function restrictedScore(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatNumber(value) : '数据受限';
}

/** 0~1 比值展示为百分比；null 显示「数据受限」。 */
export function restrictedRatio(value: number | null | undefined, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toLocaleString('zh-CN', { maximumFractionDigits: digits })}%`
    : '数据受限';
}
