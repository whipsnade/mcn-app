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

  it('shows the reviewed task activity without exposing transport details', () => {
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing
        isMockMode={false}
        taskActivity="本次调用积分已结算"
      />,
    );

    expect(screen.getByText('本次调用积分已结算')).toBeVisible();
    expect(screen.queryByText('/api/v1/mcp')).not.toBeInTheDocument();
  });

  it('shows the current backend phase and allows retrying a terminal user message', async () => {
    const onRetryMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={{ ...session, messages: [{ id: 'message-1', sender: 'user', text: '重跑这条', timestamp: '10:00', taskId: 'task-1' }] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        taskPhaseLabel="社媒数据 MCP 查询"
        taskProgress={{ current: 2, total: 3 }}
        onRetryMessage={onRetryMessage}
      />,
    );

    expect(screen.getByRole('status', { name: '任务阶段' })).toHaveTextContent('社媒数据 MCP 查询');
    expect(screen.getByText('2 / 3')).toBeVisible();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '再次执行' }));
    });
    expect(onRetryMessage).toHaveBeenCalledWith('message-1');
  });
});
