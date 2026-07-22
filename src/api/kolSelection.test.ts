import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authorizedFetch } from './client';
import { downloadKolSelection, runKolAnalysis } from './kolSelection';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

interface ClickedAnchor {
  href: string;
  download: string;
}

describe('kolSelection api', () => {
  let clicked: ClickedAnchor | undefined;

  beforeEach(() => {
    clicked = undefined;
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock-download'),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked = { href: this.href, download: this.download };
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('posts to the session kol-analysis endpoint', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({ id: 'report-1' });

    await runKolAnalysis('session-1');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session-1/kol-analysis', { method: 'POST' });
  });

  it('throws the server detail when the export fails', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ detail: 'NO_KOL_SELECTION' }),
    } as Response);

    await expect(downloadKolSelection('session-1')).rejects.toThrow('NO_KOL_SELECTION');
  });

  it('falls back to a generic error when the failure body is not json', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: false,
      status: 502,
      json: () => Promise.reject(new Error('not json')),
    } as Response);

    await expect(downloadKolSelection('session-1')).rejects.toThrow('HTTP_502');
  });

  it('downloads the sheet with the filename decoded from Content-Disposition', async () => {
    const disposition = `attachment; filename*=UTF-8''${encodeURIComponent('达人圈选_20260722.xlsx')}`;
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: true,
      headers: new Headers({ 'Content-Disposition': disposition }),
      blob: () => Promise.resolve(new Blob(['xlsx'])),
    } as Response);

    await downloadKolSelection('session-1');

    expect(authorizedFetch).toHaveBeenCalledWith('/api/v1/sessions/session-1/kol-selection/export');
    expect(clicked?.href).toBe('blob:mock-download');
    expect(clicked?.download).toBe('达人圈选_20260722.xlsx');
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('falls back to the default filename without a filename* parameter', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: true,
      headers: new Headers({ 'Content-Disposition': 'attachment' }),
      blob: () => Promise.resolve(new Blob(['xlsx'])),
    } as Response);

    await downloadKolSelection('session-1');

    expect(clicked?.download).toBe('KOL匹配度分析.xlsx');
  });
});
