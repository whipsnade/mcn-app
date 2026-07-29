import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import TaskFlowNodes from './TaskFlowNodes';
import type { TaskFlowNode } from '../state/taskEvents';
import type { ThinkingBlock } from '../types';

describe('TaskFlowNodes', () => {
  it('在新任务开始时重置上一任务完成后的收起状态', () => {
    const nodes: TaskFlowNode[] = [
      { id: 'accepted', label: '任务已受理', status: 'succeeded' },
    ];
    const { rerender } = render(
      <TaskFlowNodes taskId="task-finished" nodes={nodes} terminal terminalLabel="分析完成" />,
    );

    expect(screen.getByRole('button', { name: /执行流程 · 共 1 步/ }))
      .toHaveAttribute('aria-expanded', 'false');

    rerender(<TaskFlowNodes taskId="task-next" nodes={nodes} terminal={false} />);

    expect(screen.getByRole('button', { name: '执行流程' }))
      .toHaveAttribute('aria-expanded', 'true');
  });

  it('renders both the detail and the upstream detail on a failed node', () => {
    const nodes: TaskFlowNode[] = [
      {
        id: 'tool-1',
        label: '查询小红书数据',
        status: 'failed',
        detail: '社媒数据服务返回错误，请稍后重试。',
        upstreamDetail: 'HTTP 502 Bad Gateway: datatap gateway timeout',
      },
    ];

    render(<TaskFlowNodes nodes={nodes} />);

    expect(screen.getByText('社媒数据服务返回错误，请稍后重试。')).toBeInTheDocument();
    expect(screen.getByText('HTTP 502 Bad Gateway: datatap gateway timeout')).toBeInTheDocument();
  });

  it('renders only the detail line when a failed node has no upstream detail', () => {
    const nodes: TaskFlowNode[] = [
      {
        id: 'tool-1',
        label: '查询小红书数据',
        status: 'failed',
        detail: '社媒数据服务返回错误，请稍后重试。',
      },
    ];

    const { container } = render(<TaskFlowNodes nodes={nodes} />);

    expect(screen.getByText('社媒数据服务返回错误，请稍后重试。')).toBeInTheDocument();
    // detail 行之外没有第二行错误文本（upstreamDetail 缺失时不渲染）。
    expect(container.querySelectorAll('li p')).toHaveLength(2); // label + detail
  });
});

describe('TaskFlowNodes 思考节点合并', () => {
  const flowNodesFixture: TaskFlowNode[] = [
    { id: 'accepted', label: '任务已受理', status: 'succeeded' },
    { id: 'tool-1', label: '工具一', status: 'succeeded' },
    { id: 'tool-2', label: '工具二', status: 'succeeded' },
    { id: 'terminal', label: '分析完成', status: 'succeeded' },
  ];

  function makeThinkingBlock(
    purpose: string,
    label: string,
    changes: Partial<ThinkingBlock> = {},
  ): ThinkingBlock {
    return {
      operationId: `op-${label}`,
      turnId: 'turn-1',
      purpose,
      attempt: 1,
      label,
      content: `${label}的内容`,
      status: 'completed',
      durationMs: 1200,
      truncated: false,
      ...changes,
    };
  }

  function expectDomOrder(container: HTMLElement, labels: string[]) {
    const text = container.querySelector('ul')?.textContent ?? '';
    let lastIndex = -1;
    labels.forEach(label => {
      const index = text.indexOf(label);
      expect(index, `期望「${label}」出现在正确位置`).toBeGreaterThan(lastIndex);
      lastIndex = index;
    });
  }

  it('interleaves thinking blocks around tool nodes by purpose', () => {
    const { container } = render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[
          makeThinkingBlock('goal_planner', '规划块'),
          makeThinkingBlock('agent_loop', '决策一'),
          makeThinkingBlock('agent_loop', '决策二'),
          makeThinkingBlock('agent_loop', '决策三'), // 超出工具数量 → 最后工具节点之后
          makeThinkingBlock('kol_analysis', '收尾块'),
        ]}
      />,
    );

    expectDomOrder(container, [
      '任务已受理',
      '规划块',   // goal_planner → 首个工具节点之前
      '决策一',   // agent_loop 第 1 块 → 第 1 个工具节点之前
      '工具一',
      '决策二',   // agent_loop 第 2 块 → 第 2 个工具节点之前
      '工具二',
      '决策三',   // 多出的 agent_loop 块 → 最后工具节点之后、终态之前
      '收尾块',   // 收尾类 → 最后工具节点之后、终态之前
      '分析完成',
    ]);
  });

  it('orders thinking blocks by category when there are no tool nodes', () => {
    const { container } = render(
      <TaskFlowNodes
        nodes={[
          { id: 'accepted', label: '任务已受理', status: 'succeeded' },
          { id: 'terminal', label: '分析完成', status: 'succeeded' },
        ]}
        thinkingBlocks={[
          makeThinkingBlock('kol_analysis', '收尾块'),
          makeThinkingBlock('agent_loop', '决策一'),
          makeThinkingBlock('brainstorm', '规划块'),
        ]}
      />,
    );

    expectDomOrder(container, ['任务已受理', '规划块', '决策一', '收尾块', '分析完成']);
  });

  it('renders exactly the flow nodes when thinkingBlocks is empty', () => {
    const { container } = render(
      <TaskFlowNodes nodes={flowNodesFixture} thinkingBlocks={[]} />,
    );

    expect(container.querySelectorAll('ul > li')).toHaveLength(flowNodesFixture.length);
    expect(screen.queryByText(/决策/)).toBeNull();
  });

  it('collapses thinking nodes by default and expands one on click', () => {
    render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[makeThinkingBlock('agent_loop', '决策一', { content: '第一段思考' })]}
      />,
    );

    const toggle = screen.getByRole('button', { name: /决策一/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('第一段思考')).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('第一段思考')).toBeVisible();
  });

  it('shows 思考中 for running blocks and seconds for completed blocks', () => {
    render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[
          makeThinkingBlock('agent_loop', '运行中决策', { status: 'running', durationMs: undefined }),
          makeThinkingBlock('agent_loop', '完成决策', { status: 'completed', durationMs: 21808 }),
        ]}
      />,
    );

    expect(screen.getByText('思考中')).toBeInTheDocument();
    expect(screen.getByText('21.8 秒')).toBeInTheDocument();
  });

  it('marks interrupted blocks with an amber 思考中断 hint', () => {
    render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[makeThinkingBlock('agent_loop', '中断决策', { status: 'interrupted' })]}
      />,
    );

    expect(screen.getByText('思考中断')).toHaveClass('text-amber-600');
  });

  it('shows retry and truncation hints when expanded', () => {
    render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[makeThinkingBlock('agent_loop', '重试决策', {
          attempt: 2,
          truncated: true,
          content: '修正后的内容',
        })]}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /重试决策/ }));
    expect(screen.getByText('正在修正输出格式')).toBeVisible();
    expect(screen.getByText('思考内容已截断')).toBeVisible();
  });

  it('keeps the expanded state when content updates for the same operation attempt', () => {
    const block = makeThinkingBlock('agent_loop', '决策一', { content: '旧内容' });
    const { rerender } = render(
      <TaskFlowNodes nodes={flowNodesFixture} thinkingBlocks={[block]} />,
    );

    const toggle = screen.getByRole('button', { name: /决策一/ });
    fireEvent.click(toggle);
    expect(screen.getByText('旧内容')).toBeVisible();

    rerender(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[{ ...block, content: '新内容' }]}
      />,
    );

    expect(screen.getByRole('button', { name: /决策一/ })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('新内容')).toBeVisible();
  });

  it('skips completed/interrupted blocks with empty content but keeps running ones', () => {
    render(
      <TaskFlowNodes
        nodes={flowNodesFixture}
        thinkingBlocks={[
          makeThinkingBlock('agent_loop', '空完成块', { content: '   ', status: 'completed' }),
          makeThinkingBlock('agent_loop', '空中断块', { content: '', status: 'interrupted' }),
          makeThinkingBlock('agent_loop', '空运行块', {
            content: '',
            status: 'running',
            durationMs: undefined,
          }),
        ]}
      />,
    );

    // 与 ThinkingPanel 一致：空内容的终态块不渲染；running 块保留显示「思考中」。
    expect(screen.queryByRole('button', { name: /空完成块/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /空中断块/ })).toBeNull();
    expect(screen.getByRole('button', { name: /空运行块/ })).toBeInTheDocument();
    expect(screen.getByText('思考中')).toBeInTheDocument();
  });
});
