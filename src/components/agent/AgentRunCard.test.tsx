import { memo } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { initialRunRuntime, type RunRuntimeState } from '../../state/agentEvents';
import AgentRunCard, { AgentRunCardImpl, type AgentRunCardProps } from './AgentRunCard';
import AgentThinking from './AgentThinking';

function run(overrides: Partial<RunRuntimeState> = {}): RunRuntimeState {
  return {
    ...initialRunRuntime(overrides.runId ?? 'run-1'),
    status: 'running',
    ...overrides,
  };
}

describe('AgentRunCard', () => {
  it('renders two independent run cards and a completed run does not absorb the next run', () => {
    const completedRun = run({
      runId: 'run-1',
      status: 'completed',
      steps: [
        { id: 'run', label: '开始执行', status: 'succeeded' },
        { id: 'terminal-1', label: '分析完成', status: 'succeeded' },
      ],
      toolCalls: [{ id: 'tool-1', name: 'brand_search', status: 'succeeded' }],
    });
    const nextRun = run({
      runId: 'run-2',
      status: 'running',
      toolCalls: [{ id: 'tool-2', name: 'kol_feed', status: 'running' }],
    });
    const { rerender } = render(
      <>
        <AgentRunCard run={completedRun} />
        <AgentRunCard run={nextRun} />
      </>,
    );

    // 完成 Run 收缩为自身摘要；实时 Run 展示自己的工具步骤。
    const completedCard = screen.getByRole('button', { name: /执行卡/ });
    expect(completedCard).toHaveTextContent('共 2 步');
    expect(completedCard).toHaveTextContent('分析完成');
    expect(screen.getByText('kol_feed')).toBeVisible();
    expect(completedCard).not.toHaveTextContent('kol_feed');

    // 下一轮事件只更新第二张卡：完成卡数据不被吸收。
    rerender(
      <>
        <AgentRunCard run={completedRun} />
        <AgentRunCard run={{
          ...nextRun,
          status: 'completed',
          steps: [
            { id: 'run', label: '开始执行', status: 'succeeded' },
            { id: 'tool-2', label: '查询kol_feed', status: 'succeeded' },
            { id: 'terminal-2', label: '分析完成', status: 'succeeded' },
          ],
        }} />
      </>,
    );
    const cards = screen.getAllByRole('button', { name: /执行卡/ });
    expect(cards).toHaveLength(2);
    // 第一张完成卡仍只展示自己的步骤数与终态。
    expect(cards[0]).toHaveTextContent('共 2 步');
    expect(cards[0]).toHaveTextContent('分析完成');
    expect(cards[1]).toHaveTextContent('共 3 步');
  });

  it('keeps live thinking folded until the user expands it', () => {
    const { rerender } = render(
      <AgentRunCard
        run={run({
          status: 'running',
          hasThinking: true,
          thinkingStatus: 'running',
          thinking: '正在检索品牌数据',
        })}
      />,
    );
    const thinkingToggle = screen.getByRole('button', { name: '思考中' });
    expect(thinkingToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('正在检索品牌数据')).toBeNull();

    // 用户展开后，后续 thinking 增量不得擅自切换展开状态。
    fireEvent.click(thinkingToggle);
    rerender(
      <AgentRunCard
        run={run({
          status: 'running',
          hasThinking: true,
          thinkingStatus: 'running',
          thinking: '正在检索品牌数据，再交叉匹配达人',
        })}
      />,
    );
    expect(screen.getByText('正在检索品牌数据，再交叉匹配达人')).toBeVisible();

    // Run 卡可按原有终态规则收起；Thinking 的用户选择不由状态变化改写。
    rerender(
      <AgentRunCard
        run={run({
          status: 'completed',
          hasThinking: true,
          thinkingStatus: 'completed',
          thinking: '正在检索品牌数据，再交叉匹配达人',
          steps: [{ id: 'terminal-1', label: '分析完成', status: 'succeeded' }],
        })}
      />,
    );
    expect(screen.queryByText('正在检索品牌数据，再交叉匹配达人')).toBeNull();
    expect(screen.getByRole('button', { name: /执行卡/ })).toHaveTextContent('分析完成');
  });

  it('renders a non-expandable 正在处理 status when the run has no thinking', () => {
    render(
      <AgentRunCard
        run={run({
          status: 'running',
          hasThinking: false,
          thinking: '',
        })}
      />,
    );
    expect(screen.getByText('正在处理')).toBeVisible();
    expect(screen.queryByRole('button', { name: /思考/ })).toBeNull();
    // 不编造推理：无思考文本。
    expect(screen.queryByText(/正在检索|推理过程/)).toBeNull();
  });

  it('relabels a settled thinking state without content as 未生成思考', () => {
    render(<AgentThinking text="" hasThinking={false} status="completed" />);
    expect(screen.getByText('未生成思考')).toBeVisible();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('does not show a processing spinner when a terminal run has no thinking', () => {
    render(
      <AgentRunCard
        run={run({
          status: 'completed',
          hasThinking: false,
          thinking: '',
          steps: [{ id: 'terminal-1', label: '分析完成', status: 'succeeded' }],
        })}
      />,
    );
    // 终态卡展开后思考区不再渲染“正在处理”旋转占位。
    fireEvent.click(screen.getByRole('button', { name: /执行卡/ }));
    expect(screen.queryByText('正在处理')).toBeNull();
    expect(screen.queryByText('未生成思考')).toBeNull();
  });

  it('labels a terminal run without replayed steps as 历史执行记录 instead of 共 0 步', () => {
    render(<AgentRunCard run={run({ status: 'completed', steps: [], toolCalls: [] })} />);

    // 折叠摘要不渲染误导性的“共 0 步”。
    const collapsed = screen.getByRole('button', { name: /历史执行记录/ });
    expect(collapsed).toHaveTextContent('分析完成');
    expect(collapsed).not.toHaveTextContent('共 0 步');

    // 展开后展示「暂未回放」说明，而不是空白。
    fireEvent.click(collapsed);
    expect(screen.getByText('该历史执行详情暂未回放')).toBeVisible();
  });

  it('default export is a memoized run card', () => {
    expect((AgentRunCard as unknown as { $$typeof?: symbol }).$$typeof).toBe(Symbol.for('react.memo'));
  });

  it('memoizes the run card so a completed run is not re-rendered by the next run delta', () => {
    const impl = vi.fn((props: AgentRunCardProps) => <AgentRunCardImpl {...props} />);
    const MemoizedCard = memo(impl);
    const noop = () => undefined;
    const completedRun = run({
      runId: 'run-1',
      status: 'completed',
      steps: [
        { id: 'run', label: '开始执行', status: 'succeeded' },
        { id: 'terminal-1', label: '分析完成', status: 'succeeded' },
      ],
    });
    const activeRun = run({
      runId: 'run-2',
      status: 'running',
      toolCalls: [{ id: 'tool-1', name: 'brand_search', status: 'running' }],
    });
    const { rerender } = render(
      <>
        <MemoizedCard run={completedRun} onResume={noop} />
        <MemoizedCard run={activeRun} onResume={noop} />
      </>,
    );
    expect(impl).toHaveBeenCalledTimes(2);

    // 新 Run 增量：活跃卡 props 变化重渲染，完成卡 props 相同被 memo 跳过。
    rerender(
      <>
        <MemoizedCard run={completedRun} onResume={noop} />
        <MemoizedCard run={{ ...activeRun, thinking: '更多增量' }} onResume={noop} />
      </>,
    );
    expect(impl).toHaveBeenCalledTimes(3);
    expect(screen.getByText('brand_search')).toBeVisible();
    expect(screen.getByRole('button', { name: /执行卡/ })).toHaveTextContent('共 2 步');
  });

  it('shows safe tool names/status/duration/points without raw parameters', () => {
    render(
      <AgentRunCard
        run={run({
          status: 'running',
          toolCalls: [
            {
              id: 'tool-1',
              name: 'brand_search',
              status: 'succeeded',
              durationMs: 1200,
              points: 10,
              detail: 'input={"access_key":"AKIAFAKE","secret":"s3cr3t"}',
            },
            { id: 'tool-2', name: 'kol_feed', status: 'running' },
          ],
        })}
      />,
    );
    expect(screen.getByText('brand_search')).toBeVisible();
    expect(screen.getByText('成功')).toBeVisible();
    expect(screen.getByText('1.2 秒')).toBeVisible();
    expect(screen.getByText('10 积分')).toBeVisible();
    expect(screen.getByText('kol_feed')).toBeVisible();
    expect(screen.getByText('进行中')).toBeVisible();
    // 不展示原始敏感参数。
    expect(screen.queryByText(/AKIAFAKE/i)).toBeNull();
    expect(screen.queryByText(/access_key/i)).toBeNull();
    expect(screen.queryByText(/s3cr3t/i)).toBeNull();
  });

  it('shows independent artifact publication outcomes and ignores legacy review state', () => {
    render(
      <AgentRunCard
        run={run({
          status: 'completed_with_warnings',
          drafts: [
            { artifactId: 'brand', module: 'brand', version: 1, status: 'draft' },
            { artifactId: 'campaign', module: 'campaign', version: 1, status: 'published' },
            { artifactId: 'kol', module: 'kol', version: 1, status: 'validation_failed' },
            { artifactId: 'detail', module: 'kol', version: 1, status: 'failed' },
          ],
          steps: [{ id: 'terminal', label: '分析完成（部分发布失败）', status: 'succeeded' }],
          review: { artifactIds: ['brand'], status: 'running', revisions: 0 },
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /执行卡/ }));
    expect(screen.getByText('品牌报告准备中')).toBeVisible();
    expect(screen.getByText('活动报告已发布')).toBeVisible();
    expect(screen.getByText('达人名单发布校验失败')).toBeVisible();
    expect(screen.getByText('达人名单发布失败')).toBeVisible();
    expect(screen.queryByText(/审核|复核|Reviewer/i)).toBeNull();
  });

  it('shows a cancel button for a running run and calls onPause', () => {
    const onPause = vi.fn();
    render(<AgentRunCard run={run({ status: 'running' })} onPause={onPause} />);
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }));
    expect(onPause).toHaveBeenCalledOnce();
  });

  it('shows the same cancel button while a run is reviewing', () => {
    const onPause = vi.fn();
    render(<AgentRunCard run={run({ status: 'reviewing' })} onPause={onPause} />);
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }));
    expect(onPause).toHaveBeenCalledOnce();
  });

  it('shows a resume button for a paused run and calls onResume', () => {
    const onResume = vi.fn();
    render(<AgentRunCard run={run({ status: 'paused' })} onResume={onResume} />);
    const resumeButton = screen.getByRole('button', { name: '继续' });
    expect(resumeButton).toBeVisible();
    fireEvent.click(resumeButton);
    expect(onResume).toHaveBeenCalledOnce();
  });

  it('shows the question and option chips for clarification_requested and fills the input without auto-submit', () => {
    const onClarify = vi.fn();
    const onResume = vi.fn();
    render(
      <AgentRunCard
        run={run({ status: 'clarification_requested' })}
        clarification={{ question: '想看哪个品牌的分析？', options: ['海底捞', '喜茶'] }}
        onClarify={onClarify}
        onResume={onResume}
      />,
    );
    expect(screen.getByText('想看哪个品牌的分析？')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '海底捞' }));
    expect(onClarify).toHaveBeenCalledWith('海底捞');
    // 只填入输入框，不自动提交 / 不触发继续。
    expect(onResume).not.toHaveBeenCalled();
  });
});
