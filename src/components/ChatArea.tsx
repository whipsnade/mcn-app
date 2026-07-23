import React, { useState, useRef, useEffect } from 'react';
import { Send, Sparkles, ShieldAlert } from 'lucide-react';
import { Session, Message } from '../types';
import type { FollowupSuggestion } from '../api/contracts';
import TaskFlowNodes from './TaskFlowNodes';
import type { TaskFlowNode } from '../state/taskEvents';

/** 空白会话（无消息、无 followup 建议）展示的默认圈选建议，点击填入输入框。 */
const DEFAULT_SUGGESTIONS: { title: string; prompt: string }[] = [
  { title: '按品类圈选达人', prompt: '圈选某品类（如美食）近1个月表现最好的各平台达人，按互动量排序' },
  { title: '品牌提及博主圈选', prompt: '圈选近1个月内容中提及过某品牌的各平台博主，按互动量排序' },
  { title: '按受众画像圈选', prompt: '圈选粉丝以某地区某年龄段女性为主的各平台达人，适合推广某类产品' },
  { title: '按预算圈选达人', prompt: '在10万元预算内圈选适合推广某品牌的高性价比达人，给出组合建议' },
];

interface ChatAreaProps {
  session: Session;
  onSendMessage: (text: string) => Promise<unknown>;
  isAnalyzing: boolean;
  /** 是否处于 brainstorm 澄清等待中（loading 文案区分于任务分析）。 */
  isClarifying?: boolean;
  isMockMode: boolean;
  /** 当前任务的执行流程节点（竖状节点图）。 */
  flowNodes?: TaskFlowNode[];
  /** 任务是否已到终态（节点图自动收缩）。 */
  flowTerminal?: boolean;
  /** 终态摘要文案（如 分析完成 / 任务失败）。 */
  flowTerminalLabel?: string;
  /** AI 摘要的流式草稿，实时渲染在节点图下方。 */
  assistantDraft?: string;
  onRetryMessage?: (messageId: string) => Promise<unknown>;
  followupStatus?: 'pending' | 'completed' | 'failed';
  followupSuggestions?: FollowupSuggestion[];
  followupError?: string;
  onRetryFollowups?: () => Promise<unknown>;
}

export default function ChatArea({
  session,
  onSendMessage,
  isAnalyzing,
  isClarifying = false,
  isMockMode,
  flowNodes = [],
  flowTerminal = false,
  flowTerminalLabel,
  assistantDraft = '',
  onRetryMessage,
  followupStatus,
  followupSuggestions = [],
  followupError,
  onRetryFollowups,
}: ChatAreaProps) {
  const [inputText, setInputText] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 建议点击统一行为：填入输入框并聚焦，不自动提交，由用户确认后发送。
  const fillInput = (text: string) => {
    setInputText(text);
    textareaRef.current?.focus();
  };

  // Auto scroll to bottom
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [session.messages, isAnalyzing]);

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputText.trim() || isAnalyzing) return;
    const draft = inputText.trim();
    try {
      await onSendMessage(draft);
      setInputText(current => current.trim() === draft ? '' : current);
    } catch {
      // The workspace error banner explains the persistence failure; keep the draft for retry.
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void handleSend(e);
    }
  };

  const platformLabel = session.platform.split(',').map(platform => {
    const trimmed = platform.trim();
    if (trimmed === 'Xiaohongshu') return '小红书';
    if (trimmed === 'Douyin') return '抖音';
    if (trimmed === 'Bilibili') return '哔哩哔哩';
    if (trimmed === 'Weibo') return '微博';
    if (trimmed === 'Wechat') return '微信';
    return trimmed;
  }).join(' / ');

  const budgetLabel = session.budgetMin || session.budgetMax
    ? `${session.budgetMin ?? '0'}–${session.budgetMax ?? '不限'} 元`
    : '待确认';

  // 空白会话（brand/category 均空）头部只显示会话标题。
  const hasSessionMetadata = Boolean(session.brand.trim() || session.category.trim());
  const titleLabel = session.brand.trim()
    ? `${session.brand.split(' ')[0]}${session.campaignName ? ` - ${session.campaignName}` : ''}`
    : session.title;

  let latestAssistantMessageId: string | undefined;
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    if (session.messages[index].sender === 'ai') {
      latestAssistantMessageId = session.messages[index].id;
      break;
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-white border-r border-slate-200 h-full no-print">

      {/* Chat Header */}
      <div className="flex h-14 items-center justify-between border-b border-slate-100 bg-white px-6 shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xs font-bold text-slate-800 tracking-tight font-display">
              {titleLabel}
            </h1>
            <span className="text-[9px] bg-slate-100 text-slate-500 font-mono px-1.5 py-0.5 rounded border border-slate-200/40">
              {session.id}
            </span>
          </div>
          {hasSessionMetadata && (
            <p className="mt-0.5 text-[10px] text-slate-400 flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-500 animate-pulse" />
              {isAnalyzing ? '分析中' : '已完成'} • 渠道: {platformLabel} • 品类: {session.category} • 预算: {budgetLabel}
            </p>
          )}
        </div>

        <div className="flex items-center gap-2">
          {isMockMode && (
            <span className="flex items-center gap-1 text-[10px] bg-amber-50 text-amber-600 px-2 py-1 rounded-lg border border-amber-100 font-medium">
              <ShieldAlert className="h-3.5 w-3.5" />
              模拟
            </span>
          )}
        </div>
      </div>

      {/* Messages Feed */}
      <div role="log" aria-label="会话消息" className="min-h-0 flex-1 overflow-y-auto p-6 space-y-5 bg-white">
        
        {/* System welcome event banner */}
        <div className="flex justify-center">
          <div className="rounded-full bg-slate-50 border border-slate-200/40 px-3.5 py-1 text-[10px] text-slate-400 font-medium">
            AI 投流与 KOL 决策会话 {session.id} 已载入
          </div>
        </div>

        {session.messages.map((msg) => {
          const isAI = msg.sender === 'ai';
          const isSystem = msg.sender === 'system';

          if (isSystem) {
            return (
              <div key={msg.id} className="flex justify-center">
                <div className="rounded-full bg-indigo-50 border border-indigo-100 px-3.5 py-1 text-[10px] text-indigo-500 font-medium">
                  {msg.text}
                </div>
              </div>
            );
          }

          // 仅最新一条 assistant 消息的 brainstorm 选项渲染为可点 chips。
          const brainstormOptions = isAI && msg.id === latestAssistantMessageId
            ? msg.brainstorm?.options ?? []
            : [];

          return (
            <div 
              key={msg.id} 
              className={`flex items-start gap-3 max-w-[85%] ${
                isAI ? 'mr-auto' : 'ml-auto flex-row-reverse'
              }`}
            >
              {/* Avatar Icon */}
              <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full font-bold text-[10px] shadow-sm ${
                isAI 
                  ? 'bg-indigo-600 text-white' 
                  : 'bg-slate-200 text-slate-700'
              }`}>
                {isAI ? 'AI' : 'U'}
              </div>

              {/* Message Details */}
              <div className="space-y-1 flex-1">
                <div className={`flex items-center gap-2 text-[10px] text-slate-400 ${
                  isAI ? 'justify-start' : 'justify-end'
                }`}>
                  <span className="font-semibold text-slate-500">{isAI ? 'AI 分析师' : '品牌方'}</span>
                  <span>{msg.timestamp || '10:15'}</span>
                </div>

                {/* Message Bubble */}
                <div className={`rounded-2xl px-4 py-3 text-xs md:text-sm leading-relaxed ${
                  isAI 
                    ? 'bg-indigo-600 text-white rounded-tl-none shadow-md' 
                    : 'bg-slate-100 text-slate-700 rounded-tr-none border border-slate-200/50'
                }`}>
                  {/* Handle multiline text rendering nicely */}
                  <div className="whitespace-pre-line font-normal">
                    {msg.text}
                  </div>
                  {!isAI && msg.taskId && onRetryMessage && !isAnalyzing && (
                    <button
                      type="button"
                      onClick={() => void onRetryMessage(msg.id).catch(() => undefined)}
                      className="mt-2 rounded-lg border border-indigo-200 bg-white px-2.5 py-1 text-[10px] font-semibold text-indigo-600 transition hover:bg-indigo-50"
                    >
                      再次执行
                    </button>
                  )}
                </div>

                {/* Brainstorm 澄清选项 chips（样式复用进一步分析建议 chips） */}
                {brainstormOptions.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5" aria-label="澄清选项">
                    {brainstormOptions.map(option => (
                      <button
                        key={option}
                        type="button"
                        disabled={isAnalyzing}
                        onClick={() => {
                          if (!isAnalyzing) fillInput(option);
                        }}
                        className={`rounded-lg border px-2.5 py-1.5 text-[10px] font-semibold transition active:scale-95 ${isAnalyzing
                          ? 'cursor-not-allowed border-slate-100 bg-slate-50 text-slate-300'
                          : 'border-indigo-100 bg-white text-indigo-700 hover:border-indigo-300 hover:bg-indigo-50'
                        }`}
                      >
                        {option}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {/* 执行流程节点图 + AI 流式结果：终态后节点图自动收缩，只留最终回复 */}
        {(isAnalyzing || flowNodes.length > 0 || assistantDraft) && (
          <div className="flex items-start gap-3 mr-auto max-w-[85%]">
            <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full font-bold text-[10px] shadow-sm ${isAnalyzing ? 'bg-indigo-500 text-white animate-pulse' : 'bg-indigo-600 text-white'}`}>
              AI
            </div>
            <div className="space-y-2 flex-1 min-w-0">
              <div className="flex items-center gap-2 text-[10px] text-slate-400">
                <span className="font-semibold text-slate-500">AI 分析师</span>
                {isAnalyzing && <span className="text-indigo-500">分析中…</span>}
              </div>
              {flowNodes.length > 0 && (
                <TaskFlowNodes
                  nodes={flowNodes}
                  terminal={flowTerminal}
                  terminalLabel={flowTerminalLabel}
                />
              )}
              {assistantDraft ? (
                <div className="rounded-2xl rounded-tl-none bg-indigo-600 px-4 py-3 text-xs md:text-sm leading-relaxed text-white shadow-md">
                  <div className="whitespace-pre-line font-normal">
                    {assistantDraft}
                    {isAnalyzing && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse rounded-sm bg-white/70 align-middle" />}
                  </div>
                </div>
              ) : (
                isAnalyzing && (
                  <div className="rounded-2xl rounded-tl-none bg-white border border-slate-100 px-4 py-3.5 shadow-sm">
                    <div className="flex items-center gap-1.5" role="status">
                      <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce" />
                      <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce [animation-delay:0.2s]" />
                      <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce [animation-delay:0.4s]" />
                      <span className="text-xs text-slate-400 font-medium ml-1">{isClarifying ? '正在澄清需求…' : '正在分析数据并编制图表...'}</span>
                    </div>
                  </div>
                )
              )}
            </div>
          </div>
        )}

        <div ref={chatEndRef} />
      </div>

      {/* Input panel container */}
      <div className="shrink-0 p-4 bg-white border-t border-slate-100 space-y-2.5">

        {!followupStatus && session.messages.length === 0 && (
          <section aria-label="开始圈选建议" className="rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-[10px] font-bold text-indigo-600">
              <Sparkles className="h-3 w-3" />
              开始圈选建议
            </div>
            <div className="mt-2 flex gap-1.5 overflow-x-auto pb-0.5 scrollbar-none">
              {DEFAULT_SUGGESTIONS.map(suggestion => (
                <button
                  key={suggestion.title}
                  type="button"
                  disabled={isAnalyzing}
                  onClick={() => {
                    if (!isAnalyzing) fillInput(suggestion.prompt);
                  }}
                  className={`min-w-[150px] rounded-lg border px-2.5 py-2 text-left transition active:scale-95 ${isAnalyzing
                    ? 'cursor-not-allowed border-slate-100 bg-slate-50 text-slate-300'
                    : 'border-indigo-100 bg-white text-indigo-700 hover:border-indigo-300 hover:bg-indigo-50'
                  }`}
                >
                  <span className="block text-[10px] font-semibold">{suggestion.title}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {followupStatus && (
          <section aria-label="进一步分析建议" className="rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-[10px] font-bold text-indigo-600">
              <Sparkles className="h-3 w-3" />
              进一步分析建议
            </div>
            {followupStatus === 'pending' && (
              <p className="mt-2 text-[10px] text-slate-500" role="status">正在生成进一步分析建议…</p>
            )}
            {followupStatus === 'failed' && (
              <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-rose-600">
                <span>{followupError ?? '进一步分析建议暂时生成失败，请稍后重试。'}</span>
                {onRetryFollowups && (
                  <button
                    type="button"
                    className="shrink-0 rounded-lg border border-indigo-200 bg-white px-2 py-1 font-semibold text-indigo-600 hover:bg-indigo-50"
                    disabled={isAnalyzing}
                    onClick={() => void onRetryFollowups().catch(() => undefined)}
                  >
                    重试建议生成
                  </button>
                )}
              </div>
            )}
            {followupStatus === 'completed' && followupSuggestions.length === 0 && (
              <p className="mt-2 text-[10px] text-slate-500">本轮暂无可执行的进一步分析建议。</p>
            )}
            {followupStatus === 'completed' && followupSuggestions.length > 0 && (
              <div className="mt-2 flex gap-1.5 overflow-x-auto pb-0.5 scrollbar-none">
                {followupSuggestions.slice(0, 5).map((suggestion, index) => (
                  <button
                    key={`${suggestion.title}-${index}`}
                    type="button"
                    title={suggestion.rationale}
                    disabled={isAnalyzing}
                    onClick={() => {
                      if (!isAnalyzing) fillInput(suggestion.prompt);
                    }}
                    className={`min-w-[150px] rounded-lg border px-2.5 py-2 text-left transition active:scale-95 ${isAnalyzing
                      ? 'cursor-not-allowed border-slate-100 bg-slate-50 text-slate-300'
                      : 'border-indigo-100 bg-white text-indigo-700 hover:border-indigo-300 hover:bg-indigo-50'
                    }`}
                  >
                    <span className="block text-[10px] font-semibold">{suggestion.title}</span>
                    <span className="mt-1 block text-[9px] font-normal text-slate-500">{suggestion.rationale}</span>
                  </button>
                ))}
              </div>
            )}
          </section>
        )}

        <form
          aria-label="发送消息"
          onSubmit={event => void handleSend(event)}
          className="bg-slate-50 rounded-xl p-1 flex items-center border border-slate-200 focus-within:ring-2 focus-within:ring-indigo-500/20 transition duration-150"
        >
          
          <textarea
            ref={textareaRef}
            rows={1}
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isAnalyzing ? "正在进行深度多维数据分析中..." : "输入消息并向 AI 分析师提问（例如：按互动率和预算匹配度重新排序）..."}
            className="flex-1 bg-transparent border-none focus:ring-0 px-3 text-xs md:text-sm text-slate-700 placeholder-slate-400 py-2 font-normal outline-none resize-none max-h-20"
          />

          <button
            type="submit"
            disabled={!inputText.trim() || isAnalyzing}
            className={`px-4 py-2 rounded-lg text-xs font-bold text-white transition active:scale-95 ${
              inputText.trim() && !isAnalyzing
                ? 'bg-indigo-600 hover:bg-indigo-700'
                : 'bg-slate-200 text-slate-400 cursor-not-allowed'
            }`}
          >
            发送
          </button>
        </form>
        <p className="text-[10px] text-slate-400 text-center">
          💡 提示：你可以要求 AI 调整、模拟特定达人的销售转化、提升正向舆情占比，右侧分析报告将同步更新。
        </p>
      </div>

    </div>
  );
}
