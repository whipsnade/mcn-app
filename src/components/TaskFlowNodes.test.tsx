import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import TaskFlowNodes from './TaskFlowNodes';
import type { TaskFlowNode } from '../state/taskEvents';

describe('TaskFlowNodes', () => {
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
