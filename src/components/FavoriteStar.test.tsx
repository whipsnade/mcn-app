import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import FavoriteStar from './FavoriteStar';

describe('FavoriteStar', () => {
  it('announces 收藏 when inactive and calls onToggle on click', () => {
    const onToggle = vi.fn();
    render(<FavoriteStar active={false} busy={false} onToggle={onToggle} />);

    const button = screen.getByRole('button', { name: '收藏' });
    expect(button).not.toBeDisabled();

    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('announces 取消收藏 when active', () => {
    render(<FavoriteStar active busy={false} onToggle={vi.fn()} />);

    const button = screen.getByRole('button', { name: '取消收藏' });
    expect(button.querySelector('svg')).toHaveClass('fill-amber-400');
  });

  it('is disabled while busy to prevent double toggles', () => {
    const onToggle = vi.fn();
    render(<FavoriteStar active={false} busy onToggle={onToggle} />);

    const button = screen.getByRole('button', { name: '收藏' });
    expect(button).toBeDisabled();

    fireEvent.click(button);
    expect(onToggle).not.toHaveBeenCalled();
  });
});
