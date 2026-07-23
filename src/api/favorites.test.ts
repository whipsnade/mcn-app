import { beforeEach, describe, expect, it, vi } from 'vitest';

import { request } from './client';
import {
  createFavorite,
  createFavoriteByKey,
  deleteFavorite,
  deleteFavoriteByKey,
  listFavorites,
} from './favorites';

vi.mock('./client', () => ({
  request: vi.fn(),
}));

const mockRequest = vi.mocked(request);

describe('favorites api', () => {
  beforeEach(() => {
    mockRequest.mockReset();
    mockRequest.mockResolvedValue(undefined as never);
  });

  it('lists favorites', async () => {
    await listFavorites();
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/favorites');
  });

  it('creates a favorite by platform + kol_uid with snake_case body', async () => {
    await createFavoriteByKey({
      platform: 'xiaohongshu',
      kolUid: 'uid-1',
      nickname: '达人小A',
      snapshot: { followers: 120000, quoted_price_cny: 12000 },
    });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/favorites', {
      method: 'POST',
      body: JSON.stringify({
        platform: 'xiaohongshu',
        kol_uid: 'uid-1',
        nickname: '达人小A',
        snapshot: { followers: 120000, quoted_price_cny: 12000 },
      }),
    });
  });

  it('omits optional nickname/snapshot keys when not provided', async () => {
    await createFavoriteByKey({ platform: 'douyin', kolUid: 'uid-2' });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/favorites', {
      method: 'POST',
      body: JSON.stringify({ platform: 'douyin', kol_uid: 'uid-2' }),
    });
  });

  it('deletes a favorite by platform + kol_uid with encoded query params', async () => {
    await deleteFavoriteByKey('xiaohongshu', 'uid/特殊 1');

    expect(mockRequest).toHaveBeenCalledWith(
      `/api/v1/favorites?platform=xiaohongshu&kol_uid=${encodeURIComponent('uid/特殊 1')}`,
      { method: 'DELETE' },
    );
  });

  it('keeps the legacy kol_id create path', async () => {
    await createFavorite({ kol_id: 'kol-a', note: '备注' });

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/favorites', {
      method: 'POST',
      body: JSON.stringify({ kol_id: 'kol-a', note: '备注' }),
    });
  });

  it('keeps the legacy kol_id delete path', async () => {
    await deleteFavorite('kol-a');

    expect(mockRequest).toHaveBeenCalledWith('/api/v1/favorites/kol-a', { method: 'DELETE' });
  });
});
