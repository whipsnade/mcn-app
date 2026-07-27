import { useEffect, useId, useMemo, useRef, useState } from 'react';

import type { ThinkingBlock } from '../types';


export interface ThinkingPanelProps {
  blocks: ThinkingBlock[];
}


function completedTitle(blocks: ThinkingBlock[]): string {
  const durationMs = blocks.reduce((total, block) => total + (block.durationMs ?? 0), 0);
  if (durationMs <= 0) return '已思考';
  return `已思考 ${(durationMs / 1000).toFixed(1)} 秒`;
}


export default function ThinkingPanel({ blocks }: ThinkingPanelProps) {
  const visibleBlocks = useMemo(
    () => blocks.filter(block => block.content.trim().length > 0),
    [blocks],
  );
  const hasRunning = visibleBlocks.some(block => block.status === 'running');
  const hasInterrupted = visibleBlocks.some(block => block.status === 'interrupted');
  const [expanded, setExpanded] = useState(hasRunning);
  const previousRunningRef = useRef(hasRunning);
  const terminalCollapsedRef = useRef(false);
  const contentId = useId();

  useEffect(() => {
    const wasRunning = previousRunningRef.current;
    if (hasRunning && !wasRunning) {
      terminalCollapsedRef.current = false;
      setExpanded(true);
    } else if (!hasRunning && wasRunning && !terminalCollapsedRef.current) {
      terminalCollapsedRef.current = true;
      setExpanded(false);
    }
    previousRunningRef.current = hasRunning;
  }, [hasRunning]);

  const groups = useMemo(() => {
    const byLabel = new Map<string, ThinkingBlock[]>();
    visibleBlocks.forEach(block => {
      const grouped = byLabel.get(block.label) ?? [];
      grouped.push(block);
      byLabel.set(block.label, grouped);
    });
    return [...byLabel.entries()];
  }, [visibleBlocks]);

  if (visibleBlocks.length === 0) return null;

  const title = hasInterrupted
    ? '思考中断'
    : hasRunning
      ? '思考中'
      : completedTitle(visibleBlocks);

  return (
    <section
      className={`w-full overflow-hidden rounded-xl border text-left ${
        hasInterrupted
          ? 'border-amber-200 bg-amber-50/60 text-amber-900'
          : 'border-slate-200 bg-slate-50/80 text-slate-600'
      }`}
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded(current => !current)}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-[11px] font-semibold"
      >
        <span>{title}</span>
        <span aria-hidden="true" className={`transition-transform ${expanded ? 'rotate-90' : ''}`}>
          ›
        </span>
      </button>
      {expanded && (
        <div id={contentId} className="space-y-3 border-t border-current/10 px-3 py-2.5">
          {groups.map(([label, stageBlocks]) => (
            <section key={label} className="space-y-1.5">
              <h3 className="text-[10px] font-semibold text-slate-500">{label}</h3>
              {stageBlocks.map(block => (
                <div key={`${block.operationId}:${block.attempt}`} className="space-y-1">
                  {block.attempt >= 2 && (
                    <p className="text-[10px] font-medium text-amber-600">
                      正在修正输出格式
                    </p>
                  )}
                  <pre className="whitespace-pre-wrap break-words font-sans text-[11px] leading-relaxed">
                    {block.content}
                  </pre>
                  {block.truncated && (
                    <p className="text-[10px] text-slate-400">思考内容已截断</p>
                  )}
                </div>
              ))}
            </section>
          ))}
        </div>
      )}
    </section>
  );
}
