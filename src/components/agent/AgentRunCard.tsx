import { memo, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Pause, Play, ShieldCheck, Workflow } from 'lucide-react';

import {
  isTerminalRunStatus,
  type RunReviewStatus,
  type RunRuntimeState,
} from '../../state/agentEvents';
import AgentClarification from './AgentClarification';
import AgentRunSteps from './AgentRunSteps';
import AgentThinking from './AgentThinking';

/** clarification_requested 时展示的问题与选项（question/options 来自关联 assistant 消息）。 */
export interface RunClarification {
  question: string;
  options: string[];
}

export interface AgentRunCardProps {
  /** 当前 Run 的运行时状态（活跃 Run 实时更新；历史 Run 为终态冻结快照）。 */
  run: RunRuntimeState;
  /** 恢复暂停的 Run（paused → 继续）。 */
  onResume?: () => void;
  /** 暂停当前 Run。 */
  onPause?: () => void;
  /** 点击澄清选项：只填入输入框，不自动提交。 */
  onClarify?: (text: string) => void;
  /** clarification_requested 时的问题与选项 chips。 */
  clarification?: RunClarification;
}

// queued 来自设计状态机（[*] --> queued）；reducer 尚未发出 run.queued 事件，
// 保留为合法初始/兜底状态（status 缺失时卡片按执行中展示）。
const STATUS_META: Record<string, { label: string; dot: string }> = {
  queued: { label: '排队中', dot: 'bg-slate-400' },
  running: { label: '执行中', dot: 'bg-indigo-500 animate-pulse' },
  reviewing: { label: '审核中', dot: 'bg-amber-500 animate-pulse' },
  paused: { label: '已暂停', dot: 'bg-slate-400' },
  clarification_requested: { label: '等待补充信息', dot: 'bg-amber-500' },
  completed: { label: '分析完成', dot: 'bg-emerald-500' },
  failed: { label: '运行失败', dot: 'bg-rose-500' },
  cancelled: { label: '已取消', dot: 'bg-slate-400' },
};

/** Reviewer 只显示状态，不展示 Reviewer 内部思考。 */
const REVIEW_LABELS: Record<RunReviewStatus, string> = {
  running: '质量复核中',
  revision_requested: '需要补充',
  approved: '已通过',
  rejected: '未通过',
};

/**
 * 每个 Run 一张独立执行卡（design §13.1）：
 * - 已完成 Run 不被后续消息复用，卡片锚定在触发消息下方；
 * - thinking 实时展示、完成后折叠；
 * - 工具步骤只显示安全名称/状态/耗时/积分；
 * - paused 显示继续按钮；clarification_requested 显示问题与选项 chips。
 *
 * React.memo：历史 Run 卡（run/澄清/回调 props 稳定）跳过每次 SSE 增量引发的
 * 整树重渲染，只有活跃卡跟随实时状态更新。
 */
export function AgentRunCardImpl({
  run,
  onResume,
  onPause,
  onClarify,
  clarification,
}: AgentRunCardProps) {
  const terminal = isTerminalRunStatus(run.status);
  const [collapsed, setCollapsed] = useState(terminal);
  const firstRunIdRef = useRef(run.runId);

  // 切换 Run（不同卡片复用同一个组件实例时）重置收起状态。
  useEffect(() => {
    if (firstRunIdRef.current === run.runId) return;
    firstRunIdRef.current = run.runId;
    setCollapsed(false);
  }, [run.runId]);

  // 终态自动收缩为一行摘要，保留可回看。
  useEffect(() => {
    if (terminal) setCollapsed(true);
  }, [terminal]);

  const statusMeta = STATUS_META[run.status ?? ''] ?? { label: '执行中', dot: 'bg-indigo-500 animate-pulse' };
  const stepCount = run.steps.length;
  const canPause = (run.status === 'running' || run.status === 'reviewing') && onPause !== undefined;

  // 终态折叠：一行可展开摘要。历史 Run（未回放步骤）不渲染“共 0 步”的误导文案，
  // 改用「历史执行记录」；后续可在此按 runId 回放 SSE 以展示完整步骤（见 buildRunHistory 注释）。
  if (collapsed) {
    const summaryLabel = terminal && stepCount === 0
      ? '历史执行记录'
      : `执行卡 · 共 ${stepCount} 步`;
    return (
      <button
        type="button"
        aria-expanded="false"
        onClick={() => setCollapsed(false)}
        className="flex w-full items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2 text-left transition hover:bg-slate-100"
      >
        <Workflow className="h-3.5 w-3.5 shrink-0 text-indigo-500" aria-hidden="true" />
        <span className="flex-1 text-[11px] font-medium text-slate-600">
          {summaryLabel}
          <span className="text-slate-400"> · {statusMeta.label}</span>
        </span>
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
      </button>
    );
  }

  return (
    <section aria-label="执行卡" className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {/* 状态头部 + 暂停/继续/收起 */}
      <div className="flex items-center gap-2 border-b border-slate-100 px-3.5 py-2.5">
        <span className={`h-2 w-2 shrink-0 rounded-full ${statusMeta.dot}`} aria-hidden="true" />
        <span className="text-[11px] font-semibold text-slate-700">{statusMeta.label}</span>
        {run.activity && <span className="truncate text-[10px] text-slate-400">{run.activity}</span>}
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {canPause && (
            <button
              type="button"
              aria-label="暂停"
              onClick={onPause}
              className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600 transition hover:bg-slate-50"
            >
              <Pause className="h-3 w-3" aria-hidden="true" />
              暂停
            </button>
          )}
          {run.status === 'paused' && onResume && (
            <button
              type="button"
              aria-label="继续"
              onClick={onResume}
              className="flex items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-600 px-2 py-1 text-[10px] font-semibold text-white transition hover:bg-indigo-500"
            >
              <Play className="h-3 w-3" aria-hidden="true" />
              继续
            </button>
          )}
          <button
            type="button"
            aria-label="收起"
            onClick={() => setCollapsed(true)}
            className="rounded-md p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
          >
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* 内容区：思考 + 工具步骤 + Reviewer + 澄清 + 错误 */}
      <div className="space-y-2.5 px-3.5 py-3">
        {(run.hasThinking || !terminal) && (
          <AgentThinking text={run.thinking} hasThinking={run.hasThinking} status={run.thinkingStatus} />
        )}
        <AgentRunSteps toolCalls={run.toolCalls} />
        {/* 历史 Run 未回放步骤时的展开说明（避免展开后空白误导）。 */}
        {terminal && stepCount === 0 && (
          <p className="rounded-lg bg-slate-50 px-3 py-2 text-[10px] leading-4 text-slate-400">
            该历史执行详情暂未回放
          </p>
        )}
        {run.review && (
          <div className="flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-indigo-500" aria-hidden="true" />
            <span className="text-[11px] font-medium text-slate-600">
              质量复核：{REVIEW_LABELS[run.review.status] ?? run.review.status}
            </span>
          </div>
        )}
        {run.status === 'clarification_requested' && clarification && (
          <AgentClarification
            question={clarification.question}
            options={clarification.options}
            onSelect={onClarify}
          />
        )}
        {run.status === 'failed' && run.errorMessage && (
          <p className="text-[11px] leading-5 text-rose-600" role="alert">
            {run.errorMessage}
          </p>
        )}
      </div>
    </section>
  );
}

export default memo(AgentRunCardImpl);
