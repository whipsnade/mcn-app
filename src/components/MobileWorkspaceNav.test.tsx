import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import MobileWorkspaceNav from './MobileWorkspaceNav';


describe('MobileWorkspaceNav', () => {
  it('switches between sessions, chat, and BI without changing desktop content', () => {
    const onChange = vi.fn();
    render(<MobileWorkspaceNav active="sessions" onChange={onChange} />);

    expect(screen.getByRole('button', { name: '会话' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: '分析对话' }));
    fireEvent.click(screen.getByRole('button', { name: 'BI 报告' }));

    expect(onChange).toHaveBeenNthCalledWith(1, 'chat');
    expect(onChange).toHaveBeenNthCalledWith(2, 'bi');
  });
});
