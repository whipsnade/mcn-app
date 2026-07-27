import { Fragment, useEffect, useId, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Lightbulb, Loader2, Workflow } from 'lucide-react';

import type { TaskFlowNode, TaskFlowNodeStatus } from '../state/taskEvents';
import type { ThinkingBlock } from '../types';

interface TaskFlowNodesProps {
  nodes: TaskFlowNode[];
  /** 任务已到终态：节点图自动收缩为一行摘要。 */
  terminal?: boolean;
  /** 终态摘要文案（如 分析完成 / 任务失败）。 */
  terminalLabel?: string;
  /** 活跃 turn 的思考块：作为节点穿插进执行流程（默认折叠）。 */
  thinkingBlocks?: ThinkingBlock[];
}

const dotClass: Record<TaskFlowNodeStatus, string> = {
  running: 'bg-indigo-500 animate-pulse',
  succeeded: 'bg-emerald-500',
  failed: 'bg-rose-500',
  unknown: 'bg-amber-500',
};

/** 规划类 purpose：插在首个工具节点之前。 */
const PLANNING_PURPOSES = new Set(['goal_planner', 'brainstorm']);

function isToolNode(node: TaskFlowNode): boolean {
  return node.id.startsWith('tool-');
}

function isTerminalNode(node: TaskFlowNode): boolean {
  return node.id === 'terminal' || node.id === 'report';
}

function thinkingNodeKey(block: ThinkingBlock): string {
  return `${block.operationId}:${block.attempt}`;
}

type FlowItem =
  | { kind: 'flow'; node: TaskFlowNode }
  | { kind: 'thinking'; block: ThinkingBlock };

/**
 * 把思考块按启发式规则穿插进流节点（思考与工具事件是两条 SSE 流，无共享时钟）：
 * - goal_planner / brainstorm → 首个工具节点之前；
 * - agent_loop 第 i 块 → 第 i 个工具节点（按出现顺序计数）之前，多出的排最后工具节点之后；
 * - 其余收尾类 purpose → 最后一个工具节点之后、终态节点之前；
 * - 无工具节点时按 规划 → agent_loop → 收尾 的类别顺序排列。
 */
function mergeThinkingIntoFlow(nodes: TaskFlowNode[], blocks: ThinkingBlock[]): FlowItem[] {
  if (blocks.length === 0) return nodes.map(node => ({ kind: 'flow', node }));

  const planning = blocks.filter(block => PLANNING_PURPOSES.has(block.purpose));
  const agentLoop = blocks.filter(block => block.purpose === 'agent_loop');
  const tail = blocks.filter(
    block => !PLANNING_PURPOSES.has(block.purpose) && block.purpose !== 'agent_loop',
  );

  const toolIndices = nodes
    .map((node, index) => (isToolNode(node) ? index : -1))
    .filter(index => index >= 0);
  const firstTerminalIndex = nodes.findIndex(isTerminalNode);
  // 无工具节点时的兜底锚点：终态节点之前，没有终态节点则在末尾。
  const fallbackAnchor = firstTerminalIndex >= 0 ? firstTerminalIndex : nodes.length;
  const planningAnchor = toolIndices.length > 0 ? toolIndices[0] : fallbackAnchor;
  const tailAnchor = toolIndices.length > 0
    ? toolIndices[toolIndices.length - 1] + 1
    : fallbackAnchor;

  // anchor -> 插在 nodes[anchor] 之前的思考块（anchor === nodes.length 表示追加到末尾）。
  const anchored = new Map<number, ThinkingBlock[]>();
  const anchorAt = (anchor: number, block: ThinkingBlock) => {
    const list = anchored.get(anchor) ?? [];
    list.push(block);
    anchored.set(anchor, list);
  };

  planning.forEach(block => anchorAt(planningAnchor, block));
  agentLoop.forEach((block, index) => {
    anchorAt(index < toolIndices.length ? toolIndices[index] : tailAnchor, block);
  });
  tail.forEach(block => anchorAt(tailAnchor, block));

  const items: FlowItem[] = [];
  nodes.forEach((node, index) => {
    anchored.get(index)?.forEach(block => items.push({ kind: 'thinking', block }));
    items.push({ kind: 'flow', node });
  });
  anchored.get(nodes.length)?.forEach(block => items.push({ kind: 'thinking', block }));
  return items;
}

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
        {node.upstreamDetail && (
          <p className="mt-0.5 font-mono text-[10px] leading-4 text-slate-500">
            {node.upstreamDetail}
          </p>
        )}
      </div>
    </li>
  );
}

interface ThinkingFlowNodeProps {
  block: ThinkingBlock;
  expanded: boolean;
  onToggle: () => void;
}

/** 思考节点：灯泡图标 + label，默认折叠，展开显示思考内容（与 ThinkingPanel 同规格）。 */
function ThinkingFlowNode({ block, expanded, onToggle }: ThinkingFlowNodeProps) {
  const contentId = useId();
  return (
    <li className="relative flex items-start gap-2.5 pl-5">
      <Lightbulb
        className={`absolute left-[-2px] top-[4px] h-3 w-3 ${
          block.status === 'interrupted'
            ? 'text-amber-500'
            : block.status === 'running'
              ? 'animate-pulse text-indigo-500'
              : 'text-slate-400'
        }`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={onToggle}
          className="flex w-full items-center gap-1.5 text-left text-[11px] font-medium text-slate-700"
        >
          {block.status === 'running' && (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-indigo-500" />
          )}
          <span>{block.label}</span>
          {block.status === 'running' && (
            <span className="text-[10px] font-normal text-indigo-500">思考中</span>
          )}
          {block.status === 'completed' && block.durationMs != null && (
            <span className="text-[10px] font-normal text-slate-400">
              {`${(block.durationMs / 1000).toFixed(1)} 秒`}
            </span>
          )}
          {block.status === 'interrupted' && (
            <span className="text-[10px] font-normal text-amber-600">思考中断</span>
          )}
          <ChevronRight
            className={`ml-auto h-3 w-3 shrink-0 text-slate-400 transition-transform ${expanded ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
        </button>
        {expanded && (
          <div id={contentId} className="mt-1 space-y-1">
            {block.attempt >= 2 && (
              <p className="text-[10px] font-medium text-amber-600">正在修正输出格式</p>
            )}
            <pre className="whitespace-pre-wrap break-words font-sans text-[11px] leading-relaxed text-slate-600">
              {block.content}
            </pre>
            {block.truncated && (
              <p className="text-[10px] text-slate-400">思考内容已截断</p>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

export default function TaskFlowNodes({
  nodes,
  terminal = false,
  terminalLabel,
  thinkingBlocks,
}: TaskFlowNodesProps) {
  const [collapsed, setCollapsed] = useState(false);
  // 已展开的思考节点集合（nodeKey = operationId:attempt），默认空 = 全折叠；
  // content 更新不改变 key，展开态稳定保留。
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(new Set());

  // 任务结束（终态）时自动收缩节点图，只留一行可展开的摘要。
  useEffect(() => {
    if (terminal) setCollapsed(true);
  }, [terminal]);

  const failedCount = useMemo(
    () => nodes.filter(node => node.status === 'failed' || node.status === 'unknown').length,
    [nodes],
  );

  const items = useMemo(
    () => mergeThinkingIntoFlow(nodes, thinkingBlocks ?? []),
    [nodes, thinkingBlocks],
  );

  const toggleThinking = (key: string) => {
    setExpandedThinking(current => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

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
        {items.map(item => {
          if (item.kind === 'flow') {
            return <Fragment key={item.node.id}>{FlowNodeRow({ node: item.node })}</Fragment>;
          }
          const key = thinkingNodeKey(item.block);
          return (
            <Fragment key={`thinking-${key}`}>
              <ThinkingFlowNode
                block={item.block}
                expanded={expandedThinking.has(key)}
                onToggle={() => toggleThinking(key)}
              />
            </Fragment>
          );
        })}
      </ul>
    </section>
  );
}

export { TaskFlowNodes };
