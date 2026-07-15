import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import NewSessionModal from './NewSessionModal';


describe('NewSessionModal', () => {
  it('allows creating a session without a campaign name', async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);

    render(
      <NewSessionModal
        isOpen
        onClose={vi.fn()}
        onCreate={onCreate}
      />,
    );

    expect(screen.getByText('活动/项目名称')).toBeTruthy();
    expect(screen.queryByText('活动/项目名称 *')).toBeNull();

    fireEvent.change(screen.getByPlaceholderText('例如：雅诗兰黛'), {
      target: { value: '科颜氏' },
    });
    fireEvent.change(screen.getByPlaceholderText('例如：美妆护肤'), {
      target: { value: '护肤' },
    });
    fireEvent.change(screen.getByPlaceholderText('例如：25-35 岁一线城市女性'), {
      target: { value: '25~30' },
    });
    fireEvent.click(screen.getByRole('button', { name: '立即创建' }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      brand: '科颜氏',
      campaignName: '',
    }));
  });
});
