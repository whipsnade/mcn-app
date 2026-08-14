/** Agent Run 澄清块（design §13.1：clarification_requested 显示问题和选项 chips）。 */
export interface AgentClarificationProps {
  question: string;
  options: string[];
  disabled?: boolean;
  /** 点击选项回调：只填入输入框，不自动提交，由用户确认后发送。 */
  onSelect?: (text: string) => void;
}

export default function AgentClarification({
  question,
  options,
  disabled = false,
  onSelect,
}: AgentClarificationProps) {
  if (options.length === 0) return null;

  return (
    <section aria-label="澄清问题" className="space-y-1.5">
      <p className="text-[11px] font-medium leading-5 text-slate-700">{question}</p>
      <div className="flex flex-wrap gap-1.5">
        {options.map(option => (
          <button
            key={option}
            type="button"
            disabled={disabled}
            onClick={() => onSelect?.(option)}
            className="rounded-lg border border-indigo-100 bg-white px-2.5 py-1.5 text-[10px] font-semibold text-indigo-700 transition hover:border-indigo-300 hover:bg-indigo-50 active:scale-95"
          >
            {option}
          </button>
        ))}
      </div>
    </section>
  );
}
