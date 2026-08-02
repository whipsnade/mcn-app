import { useEffect, useId, useRef, useState } from 'react';
import { ChevronRight, Loader2 } from 'lucide-react';

/** Agent Run 的思考块（design §13.1：thinking 实时展示，完成后默认折叠）。 */
export interface AgentThinkingProps {
  /** 已累积的思考文本；收到 thinking.* 事件后由 reducer 追加。 */
  text: string;
  /** 是否收到过 thinking.* 事件；false 时不渲染可展开思考区（避免编造推理）。 */
  hasThinking: boolean;
  /** 思考流状态。 */
  status?: 'running' | 'completed' | 'interrupted';
}

const TITLE: Record<NonNullable<AgentThinkingProps['status']>, string> = {
  running: '思考中',
  completed: '已思考',
  interrupted: '思考中断',
};

/**
 * 思考区：流式期间自动展开实时展示，结束后自动折叠；
 * 完全没有 thinking 事件时只渲染一个不可展开的「正在处理」状态行。
 */
export default function AgentThinking({ text, hasThinking, status }: AgentThinkingProps) {
  const [expanded, setExpanded] = useState(status === 'running');
  const previousRunningRef = useRef(status === 'running');
  const terminalCollapsedRef = useRef(false);
  const contentId = useId();

  // 运行 → 完成/中断：自动折叠；恢复为运行：重新展开。
  useEffect(() => {
    const wasRunning = previousRunningRef.current;
    const isRunning = status === 'running';
    if (isRunning && !wasRunning) {
      terminalCollapsedRef.current = false;
      setExpanded(true);
    } else if (!isRunning && wasRunning && !terminalCollapsedRef.current) {
      terminalCollapsedRef.current = true;
      setExpanded(false);
    }
    previousRunningRef.current = isRunning;
  }, [status]);

  // 无 thinking 事件：渲染通用「正在处理」，不可展开、无思考文本。
  if (!hasThinking) {
    return (
      <div className="flex items-center gap-1.5" role="status">
        <Loader2 className="h-3 w-3 animate-spin text-indigo-500" />
        <span className="text-[11px] font-medium text-slate-500">正在处理</span>
      </div>
    );
  }

  const title = status ? TITLE[status] : '已思考';

  return (
    <section className="overflow-hidden rounded-lg border border-slate-200/70 bg-slate-50/60">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded(current => !current)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] font-semibold text-slate-600"
      >
        {status === 'running' && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-indigo-500" />}
        <span>{title}</span>
        <ChevronRight
          className={`ml-auto h-3 w-3 shrink-0 text-slate-400 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
      </button>
      {expanded && (
        <div id={contentId} className="border-t border-slate-200/60 px-2.5 py-2">
          <pre className="whitespace-pre-wrap break-words font-sans text-[11px] leading-relaxed text-slate-600">
            {text}
          </pre>
        </div>
      )}
    </section>
  );
}
