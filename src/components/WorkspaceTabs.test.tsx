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

  it('exposes the four quick-feature tabs after favorites', () => {
    const onChange = vi.fn();
    render(<WorkspaceTabs active="posts-xhs" onChange={onChange} favoriteCount={0} />);

    for (const name of ['达人推荐', '活动评估', '小红书爆贴', '抖音爆贴']) {
      expect(screen.getByRole('tab', { name })).toBeVisible();
    }
    expect(screen.getByRole('tab', { name: '小红书爆贴' })).toHaveAttribute('aria-selected', 'true');
  });
});
