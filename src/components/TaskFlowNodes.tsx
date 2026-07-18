import { Fragment, useEffect, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, Workflow } from 'lucide-react';

import type { TaskFlowNode, TaskFlowNodeStatus } from '../state/taskEvents';

interface TaskFlowNodesProps {
  nodes: TaskFlowNode[];
  /** 任务已到终态：节点图自动收缩为一行摘要。 */
  terminal?: boolean;
  /** 终态摘要文案（如 分析完成 / 任务失败）。 */
  terminalLabel?: string;
}

const dotClass: Record<TaskFlowNodeStatus, string> = {
  running: 'bg-indigo-500 animate-pulse',
  succeeded: 'bg-emerald-500',
  failed: 'bg-rose-500',
  unknown: 'bg-amber-500',
};

function FlowNodeRow({ node }: { node: TaskFlowNode }) {
  return (
    <li className="relative flex items-start gap-2.5 pl-5">
      <span
        className={`absolute left-0 top-[5px] h-2 w-2 rounded-full ${dotClass[node.status]}`}
        aria-hidden="true"
      />
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-[11px] font-medium text-slate-700">
          {node.status === 'running' && <Loader2 className="h-3 w-3 animate-spin text-indigo-500" />}
          {node.label}
        </p>
        {node.detail && (
          <p className={`mt-0.5 text-[10px] leading-4 ${node.status === 'failed' ? 'text-rose-600' : 'text-amber-600'}`}>
            {node.detail}
          </p>
        )}
      </div>
    </li>
  );
}

export default function TaskFlowNodes({ nodes, terminal = false, terminalLabel }: TaskFlowNodesProps) {
  const [collapsed, setCollapsed] = useState(false);

  // 任务结束（终态）时自动收缩节点图，只留一行可展开的摘要。
  useEffect(() => {
    if (terminal) setCollapsed(true);
  }, [terminal]);

  const failedCount = useMemo(
    () => nodes.filter(node => node.status === 'failed' || node.status === 'unknown').length,
    [nodes],
  );

  if (nodes.length === 0) return null;

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        aria-expanded="false"
        className="flex w-full items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2 text-left transition hover:bg-slate-100"
      >
        <Workflow className="h-3.5 w-3.5 shrink-0 text-indigo-500" />
        <span className="flex-1 text-[11px] font-medium text-slate-600">
          执行流程 · 共 {nodes.length} 步
          {failedCount > 0 && <span className="text-rose-600"> · {failedCount} 步失败</span>}
          {terminalLabel && <span className="text-slate-400"> · {terminalLabel}</span>}
        </span>
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />
      </button>
    );
  }

  return (
    <section aria-label="执行流程" className="rounded-xl border border-slate-200 bg-slate-50/70 px-3.5 py-3">
      <button
        type="button"
        onClick={() => setCollapsed(true)}
        aria-expanded="true"
        className="mb-2.5 flex w-full items-center gap-2 text-left"
      >
        <Workflow className="h-3.5 w-3.5 shrink-0 text-indigo-500" />
        <span className="flex-1 text-[11px] font-semibold text-slate-700">执行流程</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
      </button>
      <ul className="relative space-y-2.5 before:absolute before:bottom-2 before:left-[3px] before:top-2 before:w-px before:bg-slate-200">
        {nodes.map(node => <Fragment key={node.id}>{FlowNodeRow({ node })}</Fragment>)}
      </ul>
    </section>
  );
}

export { TaskFlowNodes };
