import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import SessionList from './SessionList';


describe('SessionList', () => {
  it('does not report a zero balance when the wallet is unavailable', () => {
    render(
      <SessionList
        sessions={[]}
        activeSessionId=""
        onSelectSession={vi.fn()}
        onOpenNewModal={vi.fn()}
        user={{ nickname: '测试用户', role: 'user' }}
        points={null}
        onOpenRecharge={vi.fn()}
      />,
    );

    expect(screen.getByText('积分暂不可用')).toBeTruthy();
    expect(screen.queryByText(/0 \/ 5,000 点/)).toBeNull();
  });
});
