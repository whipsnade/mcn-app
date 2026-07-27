import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ThinkingBlock } from '../types';
import ThinkingPanel from './ThinkingPanel';


function block(
  content: string,
  changes: Partial<ThinkingBlock> = {},
): ThinkingBlock {
  return {
    operationId: 'operation-1',
    purpose: 'agent_loop',
    attempt: 1,
    label: '分析品牌',
    content,
    status: 'running',
    truncated: false,
    ...changes,
  };
}


describe('ThinkingPanel', () => {
  it('is expanded while running and collapses after completion', () => {
    const { rerender } = render(
      <ThinkingPanel blocks={[block('分析品牌定位')]} />,
    );
    expect(screen.getByText('分析品牌定位')).toBeVisible();

    rerender(
      <ThinkingPanel
        blocks={[block('分析品牌定位', { status: 'completed', durationMs: 21808 })]}
      />,
    );
    const toggle = screen.getByRole('button', { name: '已思考 21.8 秒' });
    expect(toggle).toBeVisible();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('分析品牌定位')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('分析品牌定位')).toBeVisible();
  });

  it('labels interrupted thinking without replacing its content', () => {
    render(
      <ThinkingPanel
        blocks={[block('已保留的推理片段', {
          status: 'interrupted',
          durationMs: 1200,
          errorCode: 'UPSTREAM_TIMEOUT',
        })]}
      />,
    );

    expect(screen.getByRole('button', { name: '思考中断' })).toBeVisible();
  });

  it('shows an output-format correction for a later attempt', () => {
    render(
      <ThinkingPanel
        blocks={[
          block('第一次输出', { status: 'completed' }),
          block('修正后的输出', {
            operationId: 'operation-1',
            attempt: 2,
          }),
        ]}
      />,
    );

    expect(screen.getByText('正在修正输出格式')).toBeVisible();
    expect(screen.getByText('修正后的输出')).toBeVisible();
  });

  it('groups multiple stages by their labels', () => {
    render(
      <ThinkingPanel
        blocks={[
          block('读取品牌资料'),
          block('匹配候选达人', {
            operationId: 'operation-2',
            label: '圈选达人',
          }),
        ]}
      />,
    );

    expect(screen.getByRole('heading', { name: '分析品牌' })).toBeVisible();
    expect(screen.getByRole('heading', { name: '圈选达人' })).toBeVisible();
  });

  it('renders model output only as text', () => {
    render(<ThinkingPanel blocks={[block('<img src=x onerror=alert(1)>')]} />);

    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeVisible();
    expect(document.querySelector('img')).toBeNull();
  });

  it('renders nothing for missing or empty thinking content', () => {
    const { container, rerender } = render(<ThinkingPanel blocks={[]} />);
    expect(container).toBeEmptyDOMElement();

    rerender(<ThinkingPanel blocks={[block('')]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
