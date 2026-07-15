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
});
