import { useId, useState } from 'react';
import { ChevronRight, Loader2 } from 'lucide-react';

/** Agent Run 的思考块（design §13.1：首次渲染默认折叠，由用户决定是否展开）。 */
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
 * 思考区：首次渲染默认折叠，后续 delta 和状态变化均不覆盖用户的展开选择；
 * 完全没有 thinking 事件时只渲染一个不可展开的「正在处理」状态行。
 */
export default function AgentThinking({ text, hasThinking, status }: AgentThinkingProps) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();

  // 无 thinking 事件：运行中渲染不可展开的「正在处理」，避免编造推理；
  // 已完成的思考流（completed/interrupted）不再显示旋转占位，改标为「未生成思考」。
  if (!hasThinking) {
    if (status === 'completed' || status === 'interrupted') {
      return (
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-medium text-slate-400">未生成思考</span>
        </div>
      );
    }
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
