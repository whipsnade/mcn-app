import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import AgentThinking from './AgentThinking';

describe('AgentThinking', () => {
  it.each(['running', 'completed', 'interrupted'] as const)(
    'keeps thinking folded when first rendered as %s',
    status => {
      render(<AgentThinking text="模型思考" hasThinking status={status} />);

      expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByText('模型思考')).toBeNull();
    },
  );

  it('preserves a user expansion across deltas and status changes', () => {
    const { rerender } = render(
      <AgentThinking text="第一段" hasThinking status="running" />,
    );
    const toggle = screen.getByRole('button', { name: '思考中' });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    rerender(<AgentThinking text="第一段\n第二段" hasThinking status="completed" />);
    expect(screen.getByRole('button', { name: '已思考' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/第一段/)).toHaveTextContent('第二段');
  });
});
