import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { WorkspaceTabs } from './WorkspaceTabs';

describe('WorkspaceTabs', () => {
  it('exposes chat and favorites with prototype styling', () => {
    render(<WorkspaceTabs active="chat" onChange={vi.fn()} favoriteCount={2} />);

    expect(screen.getByRole('tab', { name: '智能会话' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '已收藏 2' })).toBeVisible();
    expect(screen.queryByRole('tab', { name: /候选清单/ })).toBeNull();
  });
});
