import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { deleteFavorite, listFavorites } from '../api/favorites';
import FavoritesPanel from './FavoritesPanel';

vi.mock('../api/favorites', () => ({
  deleteFavorite: vi.fn(),
  listFavorites: vi.fn(),
}));

describe('FavoritesPanel', () => {
  beforeEach(() => {
    vi.mocked(listFavorites).mockResolvedValue([{
      kol_id: 'kol-a', platform: 'xiaohongshu', platform_account_id: 'xhs-a', profile_url: null,
      note: null, source_task_id: 'task-1', created_at: '2026-07-15T10:00:00Z',
    }]);
    vi.mocked(deleteFavorite).mockResolvedValue();
  });

  it('loads cross-session favorites and removes one through the star action', async () => {
    render(<FavoritesPanel refreshKey={1} />);
    expect(await screen.findByText('kol-a')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '取消收藏 kol-a' }));
    await waitFor(() => expect(deleteFavorite).toHaveBeenCalledWith('kol-a'));
    expect(screen.queryByText('kol-a')).not.toBeInTheDocument();
  });
});
