import { CheckCircle2, Loader2, MinusCircle, XCircle } from 'lucide-react';

import type { RunToolCall } from '../../state/agentEvents';

/** 工具步骤列表（design §13.1：显示安全名称、状态、耗时和积分，不展示原始敏感参数）。 */
export interface AgentRunStepsProps {
  toolCalls: RunToolCall[];
}

const STATUS_META: Record<RunToolCall['status'], { label: string; className: string; spinner?: boolean }> = {
  running: { label: '进行中', className: 'text-indigo-500', spinner: true },
  succeeded: { label: '成功', className: 'text-emerald-500' },
  failed: { label: '失败', className: 'text-rose-500' },
  unknown: { label: '未知', className: 'text-amber-500' },
};

function formatDuration(durationMs: number): string {
  return `${(durationMs / 1000).toFixed(1)} 秒`;
}

/**
 * 只渲染 state 中已有的安全摘要（工具名/状态/耗时/积分）。
 * 刻意不渲染 toolCalls[].detail：detail 可能携带工具错误原文，
 * 为遵守「不展示密钥或完整敏感参数」，错误细节由后端脱敏后在别处呈现。
 */
export default function AgentRunSteps({ toolCalls }: AgentRunStepsProps) {
  if (toolCalls.length === 0) return null;

  return (
    <ul className="space-y-1.5" aria-label="工具步骤">
      {toolCalls.map(call => {
        const meta = STATUS_META[call.status];
        const Icon = meta.spinner ? Loader2 : call.status === 'succeeded'
          ? CheckCircle2
          : call.status === 'failed'
            ? XCircle
            : MinusCircle;
        return (
          <li key={call.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <Icon className={`h-3.5 w-3.5 shrink-0 ${meta.spinner ? 'animate-spin' : ''} ${meta.className}`} aria-hidden="true" />
            <span className="font-mono text-[11px] font-medium text-slate-700">{call.name}</span>
            <span className={`text-[10px] font-medium ${meta.className}`}>{meta.label}</span>
            {call.durationMs != null && (
              <span className="text-[10px] text-slate-400">{formatDuration(call.durationMs)}</span>
            )}
            {call.points != null && (
              <span className="text-[10px] font-medium text-slate-500">{call.points} 积分</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
