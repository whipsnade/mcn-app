import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import NewSessionModal from './NewSessionModal';


describe('NewSessionModal', () => {
  it('creates with only industry and initial query, including optional KOL name', async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);

    render(
      <NewSessionModal
        isOpen
        onClose={vi.fn()}
        onCreate={onCreate}
      />,
    );

    fireEvent.change(screen.getByLabelText('行业筛选 *'), { target: { value: '美妆' } });
    fireEvent.change(screen.getByPlaceholderText('例如：李佳琦'), { target: { value: '达人A' } });
    fireEvent.change(screen.getByPlaceholderText('例如：筛选 20 位近 30 天互动稳定、女性粉丝占比高的达人，并按预算匹配度排序。'), {
      target: { value: '请筛选近 30 天活跃达人' },
    });
    fireEvent.click(screen.getByRole('button', { name: '立即创建' }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      brand: '',
      campaignName: '',
      platforms: [],
      category: '美妆',
      kolName: '达人A',
      initialQuery: '请筛选近 30 天活跃达人',
    }));
  });
});
