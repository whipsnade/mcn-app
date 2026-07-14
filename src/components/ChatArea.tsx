import React, { useState, useRef, useEffect } from 'react';
import { Paperclip, Image, Send, Sparkles, MoreVertical, ShieldAlert, Cpu } from 'lucide-react';
import { Session, Message } from '../types';

interface ChatAreaProps {
  session: Session;
  onSendMessage: (text: string) => void;
  isAnalyzing: boolean;
  isMockMode: boolean;
}

export default function ChatArea({
  session,
  onSendMessage,
  isAnalyzing,
  isMockMode
}: ChatAreaProps) {
  const [inputText, setInputText] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);

  // Auto scroll to bottom
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [session.messages, isAnalyzing]);

  const handleSend = (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputText.trim() || isAnalyzing) return;
    onSendMessage(inputText.trim());
    setInputText('');
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend(e);
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

  return (
    <div className="flex flex-1 flex-col bg-white border-r border-slate-200 h-full no-print">
      
      {/* Chat Header */}
      <div className="flex h-14 items-center justify-between border-b border-slate-100 bg-white px-6 shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xs font-bold text-slate-800 tracking-tight font-display">
              {session.brand.split(' ')[0]} - {session.campaignName}
            </h1>
            <span className="text-[9px] bg-slate-100 text-slate-500 font-mono px-1.5 py-0.5 rounded border border-slate-200/40">
              {session.id}
            </span>
          </div>
          <p className="mt-0.5 text-[10px] text-slate-400 flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-indigo-500 animate-pulse" />
            进行中 • 渠道: {platformLabel} • 品类: {session.category} • 预算: {budgetLabel}
          </p>
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
      <div className="flex-1 overflow-y-auto p-6 space-y-5 bg-white">
        
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
                </div>
              </div>
            </div>
          );
        })}

        {/* Gemini Analysing state */}
        {isAnalyzing && (
          <div className="flex items-start gap-3.5 mr-auto max-w-[80%]">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-slate-400 animate-pulse border border-dashed border-indigo-300">
              <Cpu className="h-4 w-4 animate-spin text-indigo-500" />
            </div>
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-[10px] text-slate-400">
                <span className="font-semibold text-indigo-600 animate-pulse">AI 分析师正在重构BI大盘中...</span>
              </div>
              <div className="rounded-2xl rounded-tl-none bg-white border border-slate-100 px-4 py-3.5 shadow-sm">
                <div className="flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce" />
                  <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce [animation-delay:0.2s]" />
                  <span className="h-2 w-2 rounded-full bg-indigo-500 animate-bounce [animation-delay:0.4s]" />
                  <span className="text-xs text-slate-400 font-medium ml-1">正在分析达人受众、匹配预算并编制图表数据...</span>
                </div>
              </div>
            </div>
          </div>
        )}

        <div ref={chatEndRef} />
      </div>

      {/* Input panel container */}
      <div className="p-4 bg-white border-t border-slate-100 space-y-2.5">
        
        {/* Quick Commands Tags */}
        <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
          <span className="text-[10px] font-bold text-slate-400 shrink-0 uppercase tracking-wider flex items-center gap-1">
            <Sparkles className="h-3 w-3 text-indigo-500 animate-pulse" />
            常用指令:
          </span>
          <div className="flex gap-1.5 min-w-max">
            {[
              { label: "📊 分析转化率", text: "请帮我分析本次营销活动中各个KOL的销售转化率与性价比。" },
              { label: "👥 对比各KOL表现", text: "对比分析本次合作达人的具体粉丝、互动率与情感极性表现。" },
              { label: "💰 查看MCN预算分配", text: "重点分析本次MCN机构的执行成本、ROI及性价比瓶颈分析。" },
              { label: "📈 分析舆情情感", text: "深度剖析受众的情感偏好、舆情关键词分布及潜在负面规避建议。" }
            ].map((cmd, idx) => (
              <button
                key={idx}
                type="button"
                disabled={isAnalyzing}
                onClick={() => {
                  if (!isAnalyzing) {
                    onSendMessage(cmd.text);
                  }
                }}
                className={`text-[10px] font-medium px-2.5 py-1 rounded-full border transition duration-150 active:scale-95 ${
                  isAnalyzing
                    ? 'bg-slate-50 text-slate-300 border-slate-100 cursor-not-allowed'
                    : 'bg-indigo-50/50 hover:bg-indigo-50 text-indigo-600 border-indigo-100/60 hover:border-indigo-200'
                }`}
              >
                {cmd.label}
              </button>
            ))}
          </div>
        </div>

        <form onSubmit={handleSend} className="bg-slate-50 rounded-xl p-1 flex items-center border border-slate-200 focus-within:ring-2 focus-within:ring-indigo-500/20 transition duration-150">
          
          <textarea
            rows={1}
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isAnalyzing ? "正在进行深度多维数据分析中..." : "输入消息并向 AI 分析师提问（例如：调大MCN评分为95，重新计算大盘ROI）..."}
            disabled={isAnalyzing}
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
          💡 提示：你可以要求 AI 调整、模拟特定达人的销售转化、提升正向舆情占比，右侧 BI 报表将自适应重计算。
        </p>
      </div>

    </div>
  );
}
