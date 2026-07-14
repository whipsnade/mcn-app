import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import AdminPanel from './AdminPanel';


describe('AdminPanel', () => {
  it('renders a controlled read-only state during phase one', () => {
    render(
      <AdminPanel
        isOpen
        readOnly
        onClose={vi.fn()}
        accounts={[]}
        onUpdateAccounts={vi.fn()}
        currentUserNickname="系统管理员"
      />,
    );

    expect(screen.getByText('系统管理暂为只读')).toBeTruthy();
    expect(screen.queryByText('新增账号')).toBeNull();
  });
});
