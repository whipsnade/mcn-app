import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider, useAuth } from './AuthProvider';


function Probe() {
  const auth = useAuth();
  return <span>{auth.status}:{auth.user?.nickname ?? 'none'}</span>;
}


describe('AuthProvider', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('restores a session with the refresh cookie', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'new-token' }), { status: 200 }))
        .mockResolvedValueOnce(new Response(JSON.stringify({
          id: 'user-1',
          nickname: '手机用户_5678',
          role: 'user',
          channels: ['xiaohongshu'],
        }), { status: 200 })),
    );

    render(<AuthProvider><Probe /></AuthProvider>);

    await waitFor(() => {
      expect(screen.getByText('authenticated:手机用户_5678')).toBeTruthy();
    });
  });
});
