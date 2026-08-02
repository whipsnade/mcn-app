import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { WorkspaceTabs } from './WorkspaceTabs';

describe('WorkspaceTabs', () => {
  it('顶部只保留智能会话与收藏', () => {
    const onChange = vi.fn();
    render(<WorkspaceTabs active="chat" onChange={onChange} favoriteCount={2} />);

    expect(screen.getByRole('tab', { name: '智能会话' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '已收藏 2' })).toBeVisible();
  });

  it('四个快捷入口不再出现', () => {
    render(<WorkspaceTabs active="favorites" onChange={vi.fn()} favoriteCount={0} />);

    for (const name of ['达人推荐', '活动评估', '小红书爆贴', '抖音爆贴']) {
      expect(screen.queryByRole('tab', { name })).toBeNull();
    }
  });
});
