import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import { initialRunRuntime, type RunRuntimeState } from '../state/agentEvents';
import ChatArea from './ChatArea';

function runtime(runId: string, overrides: Partial<RunRuntimeState> = {}): RunRuntimeState {
  return { ...initialRunRuntime(runId), ...overrides };
}


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

  beforeEach(() => {
    vi.mocked(Element.prototype.scrollIntoView).mockClear();
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

  it('keeps the input available while a task is running and replaces the submit button with a pause button', () => {
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
    fireEvent.click(screen.getByRole('button', { name: '暂停' }));

    expect(input.value).toBe('稍后继续分析');
    expect(screen.queryByRole('button', { name: '发送' })).toBeNull();
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it('renders an icon send button that stays disabled while the input is empty', () => {
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode
      />,
    );

    const sendButton = screen.getByRole('button', { name: '发送' });
    expect(sendButton).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/), {
      target: { value: '请分析品牌' },
    });
    expect(sendButton).toBeEnabled();
  });

  it('calls onCancelTask from the pause button without submitting the form', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const onCancelTask = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={session}
        onSendMessage={onSendMessage}
        onCancelTask={onCancelTask}
        isAnalyzing
        isMockMode
      />,
    );

    const input = screen.getByPlaceholderText(/正在进行深度多维数据分析中/) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '这条不应被提交' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '暂停' }));
    });

    expect(onCancelTask).toHaveBeenCalledOnce();
    expect(onSendMessage).not.toHaveBeenCalled();
    expect(input.value).toBe('这条不应被提交');
  });

  it('disables the pause button and labels it 正在取消 while a cancel is in flight', () => {
    const onCancelTask = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        onCancelTask={onCancelTask}
        isAnalyzing
        isCancelling
        isMockMode
      />,
    );

    const button = screen.getByRole('button', { name: '正在取消' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onCancelTask).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '暂停' })).toBeNull();
  });

  it('shows a failed run as a terminal card without exposing transport details', async () => {
    render(
      <ChatArea
        session={{
          ...session,
          messages: [{ id: 'm1', sender: 'user', text: '第一轮分析', timestamp: '10:00', runId: 'run-1' }],
        }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', {
          status: 'failed',
          errorMessage: '社媒数据服务返回错误，请稍后重试。',
          steps: [
            { id: 'run', label: '开始执行', status: 'succeeded' },
            { id: 'terminal-1', label: '运行失败', status: 'failed' },
          ],
          toolCalls: [{ id: 'tool-1', name: 'brand_search', status: 'failed' }],
        })}
      />,
    );

    // 终态收缩为摘要行，失败提示保留在展开明细里。
    const collapsed = screen.getByRole('button', { name: /执行卡/ });
    expect(collapsed).toHaveTextContent('运行失败');
    expect(screen.queryByText('/api/v1/mcp')).not.toBeInTheDocument();
    fireEvent.click(collapsed);
    expect(await screen.findByText('社媒数据服务返回错误，请稍后重试。')).toBeVisible();
    expect(screen.getByText('brand_search')).toBeVisible();
  });

  it('anchors each run card under its triggering user message; historical runs collapse, the active run is live', () => {
    render(
      <ChatArea
        session={{
          ...session,
          messages: [
            { id: 'm1', sender: 'user', text: '第一轮分析', timestamp: '10:00', runId: 'run-1' },
            { id: 'm2', sender: 'user', text: '第二轮分析', timestamp: '10:05', runId: 'run-2' },
          ],
        }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-2', {
          status: 'running',
          hasThinking: true,
          thinkingStatus: 'running',
          thinking: '正在交叉匹配达人',
          toolCalls: [{ id: 'tool-1', name: 'kol_feed', status: 'running' }],
        })}
        runHistory={{
          'run-1': runtime('run-1', {
            status: 'completed',
            steps: [
              { id: 'run', label: '开始执行', status: 'succeeded' },
              { id: 'terminal-1', label: '分析完成', status: 'succeeded' },
            ],
          }),
        }}
      />,
    );

    // 历史 run-1 收缩为终态摘要；活跃 run-2 实时展示节点与思考。
    const collapsed = screen.getByRole('button', { name: /执行卡/ });
    expect(collapsed).toHaveTextContent('分析完成');
    expect(collapsed).toHaveTextContent('共 2 步');
    expect(screen.getByText('kol_feed')).toBeVisible();
    expect(screen.getByRole('button', { name: '思考中' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('正在交叉匹配达人')).toBeNull();
    // 活跃 Run 已锚定到消息，底部不再重复渲染执行卡。
    expect(screen.getAllByLabelText('执行卡')).toHaveLength(1);
    // 历史卡锚定在第一轮消息之后、第二轮消息之前；活跃卡在第二轮消息之后。
    const secondMessage = screen.getByText('第二轮分析');
    expect(collapsed.compareDocumentPosition(secondMessage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const activeCard = screen.getByLabelText('执行卡');
    expect(secondMessage.compareDocumentPosition(activeCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows a loading placeholder while a historical run runtime is still loading', () => {
    render(
      <ChatArea
        session={{
          ...session,
          messages: [{ id: 'm1', sender: 'user', text: '历史分析', timestamp: '10:00', runId: 'run-h' }],
        }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        runHistory={{}}
      />,
    );

    expect(screen.getByText('Run 加载中…')).toBeVisible();
  });

  it('renders a replayed historical run card with tool steps and collapsed thinking', () => {
    // C3：历史 Run 经事件回放补齐完整 runtime 后，执行卡展开可回看工具步骤，
    // thinking 以「已思考」折叠区呈现。
    render(
      <ChatArea
        session={{
          ...session,
          messages: [{ id: 'm1', sender: 'user', text: '历史分析', timestamp: '10:00', runId: 'run-h' }],
        }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        runHistory={{
          'run-h': runtime('run-h', {
            status: 'completed',
            connection: 'closed',
            hasThinking: true,
            thinking: '正在检索品牌声量',
            thinkingStatus: 'completed',
            steps: [
              { id: 'run', label: '开始执行', status: 'succeeded' },
              { id: 'tool-5', label: '查询brand_search', status: 'succeeded' },
              { id: 'terminal-7', label: '分析完成', status: 'succeeded' },
            ],
            toolCalls: [{ id: 'tool-5', name: 'brand_search', status: 'succeeded' }],
          }),
        }}
      />,
    );

    // 终态折叠摘要带真实步数（回放成功不再是「历史执行记录」空壳）。
    const collapsed = screen.getByRole('button', { name: /执行卡 · 共 3 步\s*· 分析完成/ });
    fireEvent.click(collapsed);

    const card = screen.getByLabelText('执行卡');
    expect(within(card).getByText('brand_search')).toBeVisible();
    // thinking 折叠展示，展开后可读。
    fireEvent.click(within(card).getByRole('button', { name: '已思考' }));
    expect(within(card).getByText('正在检索品牌声量')).toBeVisible();
  });

  it('wires clarification chips and pause/resume through ChatArea', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const onCancelRun = vi.fn().mockResolvedValue(undefined);
    const onResumeRun = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ChatArea
        session={{
          ...session,
          messages: [
            { id: 'm1', sender: 'user', text: '帮我分析', timestamp: '10:00', runId: 'run-1' },
            { id: 'a1', sender: 'ai', text: '想看哪个品牌的分析？', timestamp: '10:01', runId: 'run-1', clarify: { options: ['海底捞', '喜茶'] } },
          ],
        }}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', { status: 'clarification_requested' })}
        onCancelRun={onCancelRun}
        onResumeRun={onResumeRun}
      />,
    );

    // 澄清 chips：点击只填入输入框，不自动提交。
    const card = screen.getByLabelText('执行卡');
    await act(async () => {
      fireEvent.click(within(card).getByRole('button', { name: '海底捞' }));
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('海底捞');

    // paused → 继续按钮调用 onResumeRun。
    rerender(
      <ChatArea
        session={{ ...session, messages: [{ id: 'm1', sender: 'user', text: '帮我分析', timestamp: '10:00', runId: 'run-1' }] }}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', { status: 'paused' })}
        onCancelRun={onCancelRun}
        onResumeRun={onResumeRun}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '继续' }));
    });
    expect(onResumeRun).toHaveBeenCalledOnce();

    // running → 暂停按钮调用 onCancelRun。
    rerender(
      <ChatArea
        session={{ ...session, messages: [{ id: 'm1', sender: 'user', text: '帮我分析', timestamp: '10:00', runId: 'run-1' }] }}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', { status: 'running' })}
        onCancelRun={onCancelRun}
        onResumeRun={onResumeRun}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '暂停' }));
    });
    expect(onCancelRun).toHaveBeenCalledOnce();
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

  it('only exposes run retry for failed runs', async () => {
    const onRetryRun = vi.fn().mockResolvedValue(undefined);
    const message = { id: 'message-1', sender: 'user' as const, text: '重跑这条', timestamp: '10:00', runId: 'run-1' };
    const { rerender } = render(
      <ChatArea
        session={{ ...session, messages: [message] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', { status: 'completed' })}
        onRetryRun={onRetryRun}
      />,
    );

    expect(screen.queryByRole('button', { name: '重试此 Run' })).toBeNull();

    rerender(
      <ChatArea
        session={{ ...session, messages: [message] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        run={runtime('run-1', { status: 'failed' })}
        onRetryRun={onRetryRun}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试此 Run' }));
    });
    expect(onRetryRun).toHaveBeenCalledWith('run-1');
  });

  it('shows ready follow-up suggestions and fills the input with the clicked prompt without sending', async () => {
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
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('请进一步分析粉丝地域分布');
  });

  it('renders utility suggestion chips under the assistant message and fills the input without sending', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={{
          ...session,
          messages: [
            { id: 'm1', sender: 'user', text: '帮我分析品牌', timestamp: '10:00', runId: 'run-1' },
            {
              id: 'a1',
              sender: 'ai',
              text: '分析完成，共圈选 12 位达人。',
              timestamp: '10:01',
              runId: 'run-1',
              suggestions: ['对比一下竞品的投放节奏', '按预算重新排序达人名单'],
            },
          ],
        }}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
        runHistory={{ 'run-1': runtime('run-1', { status: 'completed' }) }}
      />,
    );

    const region = screen.getByLabelText('追问建议');
    const chip = within(region).getByRole('button', { name: '对比一下竞品的投放节奏' });
    expect(within(region).getByRole('button', { name: '按预算重新排序达人名单' })).toBeVisible();
    await act(async () => {
      fireEvent.click(chip);
    });
    // 点击只填入输入框并聚焦，不自动提交。
    expect(onSendMessage).not.toHaveBeenCalled();
    const input = screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement;
    expect(input.value).toBe('对比一下竞品的投放节奏');
    expect(document.activeElement).toBe(input);
  });

  it('does not render suggestion chips when the assistant message has no suggestions', () => {
    render(
      <ChatArea
        session={{
          ...session,
          messages: [
            { id: 'a1', sender: 'ai', text: '第一轮分析完成。', timestamp: '10:01', runId: 'run-1' },
            { id: 'a2', sender: 'ai', text: '第二轮分析完成。', timestamp: '10:02', runId: 'run-2', suggestions: [] },
          ],
        }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.queryByLabelText('追问建议')).toBeNull();
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

  it('renders brainstorm option chips under the latest assistant message and fills the input with the clicked option', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const clarifyingSession: Session = {
      ...session,
      messages: [
        { id: 'message-u1', sender: 'user', text: '想分析新品防晒', timestamp: '10:00' },
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想分析哪个平台？',
          timestamp: '10:01',
          brainstorm: { ready: false, options: ['小红书', '抖音'] },
        },
      ],
    };
    render(
      <ChatArea
        session={clarifyingSession}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    const chip = screen.getByRole('button', { name: '小红书' });
    expect(chip).toBeVisible();
    expect(screen.getByRole('button', { name: '抖音' })).toBeVisible();
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('小红书');
  });

  it('renders planner clarify option chips and fills the input on click', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const clarifyingSession: Session = {
      ...session,
      messages: [
        { id: 'message-u1', sender: 'user', text: '帮我做个分析', timestamp: '10:00' },
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想看哪个品牌的分析？',
          timestamp: '10:01',
          clarify: { options: ['海底捞', '喜茶'] },
        },
      ],
    };
    render(
      <ChatArea
        session={clarifyingSession}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    const chip = screen.getByRole('button', { name: '海底捞' });
    expect(chip).toBeVisible();
    expect(screen.getByRole('button', { name: '喜茶' })).toBeVisible();
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('海底捞');
  });

  it('hides brainstorm options of older assistant messages once a newer one arrives', () => {
    const clarifyingSession: Session = {
      ...session,
      messages: [
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想分析哪个平台？',
          timestamp: '10:01',
          brainstorm: { ready: false, options: ['小红书', '抖音'] },
        },
        { id: 'message-u2', sender: 'user', text: '小红书', timestamp: '10:02' },
        {
          id: 'message-a2',
          sender: 'ai',
          text: '分析目标是什么？',
          timestamp: '10:03',
          brainstorm: { ready: false, options: ['声量口碑', '达人投放'] },
        },
      ],
    };
    render(
      <ChatArea
        session={clarifyingSession}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.queryByRole('button', { name: '小红书' })).toBeNull();
    expect(screen.getByRole('button', { name: '声量口碑' })).toBeVisible();
    expect(screen.getByRole('button', { name: '达人投放' })).toBeVisible();
  });

  it('shows only the session title for a blank session without brand or category', () => {
    const blank: Session = {
      ...session,
      title: '新会话1',
      brand: '',
      campaignName: null,
      category: '',
      messages: [],
    };
    render(
      <ChatArea
        session={blank}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.getByRole('heading', { name: '新会话1' })).toBeVisible();
    expect(screen.queryByText(/渠道:/)).toBeNull();
    expect(screen.queryByText(/预算:/)).toBeNull();
  });

  it('shows the clarifying hint while a brainstorm request is in flight', () => {
    render(
      <ChatArea
        session={session}
        onSendMessage={vi.fn()}
        isAnalyzing
        isClarifying
        isMockMode={false}
      />,
    );

    expect(screen.getByText('正在澄清需求…')).toBeVisible();
    expect(screen.queryByText('正在分析数据并编制图表...')).toBeNull();
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

  it('renders the four default suggestions for a blank session and fills the input on click', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatArea
        session={{ ...session, messages: [] }}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.getByText('按品类圈选达人')).toBeVisible();
    expect(screen.getByText('品牌提及博主圈选')).toBeVisible();
    expect(screen.getByText('按受众画像圈选')).toBeVisible();
    expect(screen.getByText('按预算圈选达人')).toBeVisible();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /按品类圈选达人/ }));
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('圈选某品类（如美食）近1个月表现最好的各平台达人，按互动量排序');
  });

  it('hides the default suggestions once the session has messages', () => {
    render(
      <ChatArea
        session={{ ...session, messages: [{ id: 'message-1', sender: 'user', text: '你好', timestamp: '10:00' }] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.queryByText('按品类圈选达人')).toBeNull();
  });

  it('hides the default suggestions while follow-up suggestions are active', () => {
    render(
      <ChatArea
        session={{ ...session, messages: [] }}
        onSendMessage={vi.fn()}
        isAnalyzing={false}
        isMockMode={false}
        followupStatus="pending"
      />,
    );

    expect(screen.queryByText('按品类圈选达人')).toBeNull();
    expect(screen.getByText('正在生成进一步分析建议…')).toBeVisible();
  });

  it('lets multi-select brainstorm chips toggle and joins the confirmed options into the input', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const clarifyingSession: Session = {
      ...session,
      messages: [
        { id: 'message-u1', sender: 'user', text: '想分析新品防晒', timestamp: '10:00' },
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想分析哪些平台？（可多选）',
          timestamp: '10:01',
          brainstorm: { ready: false, options: ['小红书', '抖音', '哔哩哔哩'], multi: true },
        },
      ],
    };
    render(
      <ChatArea
        session={clarifyingSession}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    const confirmButton = screen.getByRole('button', { name: '确认' });
    expect(confirmButton).toBeDisabled();

    const xiaohongshuChip = screen.getByRole('button', { name: '小红书' });
    const douyinChip = screen.getByRole('button', { name: '抖音' });
    expect(xiaohongshuChip).toHaveAttribute('aria-pressed', 'false');
    expect(douyinChip).toHaveAttribute('aria-pressed', 'false');

    await act(async () => {
      fireEvent.click(xiaohongshuChip);
      fireEvent.click(douyinChip);
    });
    expect(xiaohongshuChip).toHaveAttribute('aria-pressed', 'true');
    expect(douyinChip).toHaveAttribute('aria-pressed', 'true');
    expect(confirmButton).toBeEnabled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('');

    // 再次点击取消选中
    await act(async () => {
      fireEvent.click(xiaohongshuChip);
    });
    expect(xiaohongshuChip).toHaveAttribute('aria-pressed', 'false');

    await act(async () => {
      fireEvent.click(xiaohongshuChip);
    });
    await act(async () => {
      fireEvent.click(confirmButton);
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('抖音、小红书');
    expect(xiaohongshuChip).toHaveAttribute('aria-pressed', 'false');
    expect(douyinChip).toHaveAttribute('aria-pressed', 'false');
    expect(confirmButton).toBeDisabled();
  });

  it('keeps single-fill behavior for brainstorm chips with multi=false', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    const clarifyingSession: Session = {
      ...session,
      messages: [
        { id: 'message-u1', sender: 'user', text: '想分析新品防晒', timestamp: '10:00' },
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想分析哪个平台？',
          timestamp: '10:01',
          brainstorm: { ready: false, options: ['小红书', '抖音'], multi: false },
        },
      ],
    };
    render(
      <ChatArea
        session={clarifyingSession}
        onSendMessage={onSendMessage}
        isAnalyzing={false}
        isMockMode={false}
      />,
    );

    expect(screen.queryByRole('button', { name: '确认' })).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '小红书' }));
    });
    expect(onSendMessage).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('小红书');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '抖音' }));
    });
    expect((screen.getByPlaceholderText(/输入消息并向 AI 分析师提问/) as HTMLTextAreaElement).value)
      .toBe('抖音');
  });

  it('resets multi-select state when a new assistant message becomes the latest', async () => {
    const baseProps = {
      onSendMessage: vi.fn().mockResolvedValue(undefined),
      isAnalyzing: false,
      isMockMode: false,
    };
    const firstSession: Session = {
      ...session,
      messages: [
        {
          id: 'message-a1',
          sender: 'ai',
          text: '想分析哪些平台？（可多选）',
          timestamp: '10:01',
          brainstorm: { ready: false, options: ['小红书', '抖音'], multi: true },
        },
      ],
    };
    const { rerender } = render(<ChatArea session={firstSession} {...baseProps} />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '小红书' }));
    });
    expect(screen.getByRole('button', { name: '小红书' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '确认' })).toBeEnabled();

    const nextSession: Session = {
      ...session,
      messages: [
        {
          id: 'message-a2',
          sender: 'ai',
          text: '分析目标是什么？（可多选）',
          timestamp: '10:03',
          brainstorm: { ready: false, options: ['声量口碑', '达人投放'], multi: true },
        },
      ],
    };
    rerender(<ChatArea session={nextSession} {...baseProps} />);

    expect(screen.getByRole('button', { name: '声量口碑' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: '达人投放' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: '确认' })).toBeDisabled();
  });
});
