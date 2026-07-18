import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import ChatArea from './ChatArea';


const session: Session = {
  id: 'session-1',
  title: '测试品牌-新品筛选',
  brand: '测试品牌',
  campaignName: '新品筛选',
  status: 'draft',
  platform: 'Xiaohongshu',
  category: '美妆',
  targetAudience: '18-30 岁女性',
  summary: '寻找达人',
  messages: [],
  isStarred: false,
  createdAt: '2026-07-14T10:00:00Z',
  updatedAt: '2026-07-14T10:00:00Z',
};


describe('ChatArea', () => {
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('keeps the draft until the message is persisted successfully', async () => {
    let resolveSend: () => void = () => undefined;
    const onSendMessage = vi.fn(() => new Promise<void>(resolve => {
      resolveSend = resolve;
    }));
    render(
      <ChatArea
        session={session}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode
      />,
    );

    const input = screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '请保存这条消息' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(input.value).toBe('请保存这条消息');
    await act(async () => resolveSend());
    await waitFor(() => expect(input.value).toBe(''));
  });

  it('keeps the input available while a task is running but prevents a duplicate submit', () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={session}
        onSendMessage={onSendMessage}
        isAnalyzing
        isMockMode
      />,
    );

    const input = screen.getByPlaceholderText(/正在进行深度多维数据分析中/) as HTMLTextAreaElement;
    expect(input).toBeEnabled();
    fireEvent.change(input, { target: { value: '稍后继续分析' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(input.value).toBe('稍后继续分析');
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it('shows the flow nodes with failure detail without exposing transport details', () => {
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing
        isMockMode={false}
        flowNodes={[
          { id: 'accepted', label: '任务已受理', status: 'succeeded' },
          { id: 'tool-1', label: '查询小红书数据', status: 'failed', detail: '社媒数据服务返回错误，请稍后重试。' },
        ]}
      />,
    );

    expect(screen.getByText('查询小红书数据')).toBeVisible();
    expect(screen.getByText('社媒数据服务返回错误，请稍后重试。')).toBeVisible();
    expect(screen.queryByText('/api/v1/mcp')).not.toBeInTheDocument();
  });

  it('collapses the flow nodes after the terminal event and keeps the final reply', async () => {
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        flowTerminal
        flowTerminalLabel="分析完成"
        assistantDraft="本轮共找到 3 位候选达人。"
        flowNodes={[
          { id: 'accepted', label: '任务已受理', status: 'succeeded' },
          { id: 'tool-1', label: '查询小红书数据', status: 'failed', detail: '社媒数据服务返回错误，请稍后重试。' },
          { id: 'terminal', label: '分析完成', status: 'succeeded' },
        ]}
      />,
    );

    // 终态后自动收缩：节点明细默认不可见，摘要行可见，最终回复保留。
    expect(screen.queryByText('查询小红书数据')).not.toBeInTheDocument();
    expect(screen.getByText('本轮共找到 3 位候选达人。')).toBeVisible();
    const toggle = screen.getByRole('button', { name: /执行流程/ });
    expect(toggle).toHaveTextContent('1 步失败');
    fireEvent.click(toggle);
    expect(await screen.findByText('查询小红书数据')).toBeVisible();
    expect(screen.getByText('社媒数据服务返回错误，请稍后重试。')).toBeVisible();
  });

  it('allows retrying a terminal user message', async () => {
    const onRetryMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={{ ...session, messages: [{ id: 'message-1', sender: 'user', text: '重跑这条', timestamp: '10:00', taskId: 'task-1' }] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        onRetryMessage={onRetryMessage}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '再次执行' }));
    });
    expect(onRetryMessage).toHaveBeenCalledWith('message-1');
  });

  it('shows ready follow-up suggestions and starts the next round from a clicked prompt', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={session}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
        followupStatus="completed"
        followupSuggestions={[{ title: '分析地域', prompt: '请进一步分析粉丝地域分布', rationale: '识别重点投放区域' }]}
      />,
    );

    expect(screen.getByText('进一步分析建议')).toBeVisible();
    expect(screen.getByText('识别重点投放区域')).toBeVisible();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /分析地域/ }));
    });
    expect(onSendMessage).toHaveBeenCalledWith('请进一步分析粉丝地域分布');
  });

  it('keeps a long message history inside the fixed workspace column', () => {
    const messages = Array.from({ length: 30 }, (_, index) => ({
      id: `message-${index}`,
      sender: index % 2 === 0 ? 'user' as const : 'ai' as const,
      text: `第 ${index + 1} 轮分析结果：${'较长的分析内容。'.repeat(20)}`,
      timestamp: '10:00',
    }));
    const { container } = render(
      <ChatArea
        session={{ ...session, messages }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(container.firstElementChild).toHaveClass('min-h-0');
    expect(screen.getByRole('log', { name: '会话消息' })).toHaveClass('min-h-0', 'overflow-y-auto');
    expect(screen.getByRole('form', { name: '发送消息' }).parentElement).toHaveClass('shrink-0');
  });

  it('renders loading and retryable error states for follow-up suggestions', async () => {
    const onRetryFollowups = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ChatArea session={session} onSendMessage={vi.fn()} isAnalyzing={false} isMockMode={false} followupStatus="pending" />,
    );
    expect(screen.getByText('正在生成进一步分析建议…')).toBeVisible();

    rerender(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        followupStatus="failed"
        followupError="进一步分析建议暂时生成失败，请稍后重试。"
        onRetryFollowups={onRetryFollowups}
      />,
    );
    expect(screen.getByText('进一步分析建议暂时生成失败，请稍后重试。')).toBeVisible();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '重试建议生成' })));
    expect(onRetryFollowups).toHaveBeenCalledOnce();
  });
});
