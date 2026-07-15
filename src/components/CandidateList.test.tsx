import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { candidatePage } from '../test/fixtures';
import CandidateList from './CandidateList';

describe('CandidateList', () => {
  it('sorts candidates and keeps selected ids for comparison', () => {
    render(<CandidateList page={candidatePage} onFavorite={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: '互动率' }));
    expect(screen.getAllByTestId('candidate-name').map(node => node.textContent)).toEqual([
      '达人乙', '达人甲',
    ]);

    fireEvent.click(screen.getByRole('checkbox', { name: '选择达人乙' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '选择达人甲' }));
    expect(screen.getByRole('button', { name: '对比 2 位达人' })).toBeEnabled();
  });

  it('does not allow selecting more than four candidates', () => {
    const page = { ...candidatePage, items: Array.from({ length: 5 }, (_, index) => ({
      ...candidatePage.items[index % 2], id: `candidate-${index}`, kol_id: `kol-${index}`, nickname: `达人${index}`,
    })) };
    render(<CandidateList page={page} onFavorite={vi.fn()} />);

    page.items.forEach(item => fireEvent.click(screen.getByRole('checkbox', { name: `选择${item.nickname}` })));
    expect(screen.getByRole('button', { name: '对比 4 位达人' })).toBeEnabled();
    expect(screen.getByRole('checkbox', { name: '选择达人4' })).not.toBeChecked();
  });

  it('filters candidates by platform before comparing them', () => {
    render(<CandidateList page={candidatePage} onFavorite={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('筛选平台'), { target: { value: 'douyin' } });

    expect(screen.getAllByTestId('candidate-name').map(node => node.textContent)).toEqual(['达人乙']);
  });

  it('exposes sortable follower and price columns plus data freshness and completeness', () => {
    render(<CandidateList page={candidatePage} onFavorite={vi.fn()} />);

    expect(screen.getByRole('button', { name: '粉丝' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '价格' })).toBeEnabled();
    expect(screen.getByText(/数据完整度/)).toBeVisible();
    expect(screen.getByText(/更新于/)).toBeVisible();
  });

  it('sorts follower and price metrics with rank as a stable tie breaker', () => {
    render(<CandidateList page={candidatePage} onFavorite={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: '粉丝' }));
    expect(screen.getAllByTestId('candidate-name').map(node => node.textContent)).toEqual(['达人乙', '达人甲']);

    fireEvent.click(screen.getByRole('button', { name: '价格' }));
    expect(screen.getAllByTestId('candidate-name').map(node => node.textContent)).toEqual(['达人乙', '达人甲']);
  });

  it('keeps favorite state unchanged and reports an action failure', async () => {
    const onFavorite = vi.fn().mockRejectedValue(new Error('network'));
    render(<CandidateList page={candidatePage} onFavorite={onFavorite} />);

    fireEvent.click(screen.getByRole('button', { name: '收藏 达人甲' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('收藏操作失败');
    expect(onFavorite).toHaveBeenCalledTimes(1);
  });
});
