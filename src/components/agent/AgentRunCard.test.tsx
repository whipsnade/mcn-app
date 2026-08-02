import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { initialRunRuntime, type RunRuntimeState } from '../../state/agentEvents';
import AgentRunCard from './AgentRunCard';

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

  it('expands live thinking while streaming and collapses it on completion', () => {
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
    expect(screen.getByText('正在检索品牌数据')).toBeVisible();

    // thinking 增量继续流式，保持展开。
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

    // 完成后 Run 卡与思考区自动折叠。
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

  it.each([
    ['running', '质量复核中'],
    ['revision_requested', '需要补充'],
    ['approved', '已通过'],
    ['rejected', '未通过'],
  ] as const)('shows only the reviewer status for %s', (reviewStatus, label) => {
    render(
      <AgentRunCard
        run={run({
          status: 'reviewing',
          review: { artifactIds: ['artifact-1'], status: reviewStatus, revisions: 0 },
        })}
      />,
    );
    expect(screen.getByText(`质量复核：${label}`)).toBeVisible();
    // 不展示 Reviewer 内部思考。
    expect(screen.queryByText(/审核意见|内部判断|confidence/i)).toBeNull();
  });

  it('shows a pause button for a running run and calls onPause', () => {
    const onPause = vi.fn();
    render(<AgentRunCard run={run({ status: 'running' })} onPause={onPause} />);
    fireEvent.click(screen.getByRole('button', { name: '暂停' }));
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
