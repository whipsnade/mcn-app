import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authorizedFetch } from './client';
import { downloadBrandReport, getArtifactsSummary, listSessionReports, markArtifactRead } from './reports';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

interface ClickedAnchor {
  href: string;
  download: string;
}

describe('reports api', () => {
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
    vi.clearAllMocks();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('lists session reports filtered by report type', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue([]);

    await listSessionReports('session-1', 'brand_analysis');

    expect(request).toHaveBeenCalledWith(
      '/api/v1/sessions/session-1/reports?report_type=brand_analysis',
    );
  });

  it('fetches the artifacts summary', async () => {
    const { request } = await import('./client');
    const summary = {
      brand: { latest_artifact: null, unread: false },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: { latest_artifact: null, unread: true },
      kol_selection: { latest_artifact: null, unread: false },
    };
    vi.mocked(request).mockResolvedValue(summary);

    const result = await getArtifactsSummary('session-1');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session-1/artifacts/summary');
    expect(result).toEqual(summary);
  });

  it('marks an artifact as read', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue(undefined);

    await markArtifactRead('session-1', 'kol_analysis', 'artifact-1');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session-1/artifact-read-state', {
      method: 'PUT',
      body: JSON.stringify({ module_key: 'kol_analysis', artifact_id: 'artifact-1' }),
    });
  });

  it('downloads the brand report with the filename decoded from Content-Disposition', async () => {
    const disposition = `attachment; filename*=UTF-8''${encodeURIComponent('品牌分析报告_海底捞_2026-06-01_2026-06-30_v2.xlsx')}`;
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: true,
      headers: new Headers({ 'Content-Disposition': disposition }),
      blob: () => Promise.resolve(new Blob(['xlsx'])),
    } as Response);

    await downloadBrandReport('session-1', 'report-9');

    expect(authorizedFetch).toHaveBeenCalledWith('/api/v1/sessions/session-1/reports/report-9/export');
    expect(clicked?.href).toBe('blob:mock-download');
    expect(clicked?.download).toBe('品牌分析报告_海底捞_2026-06-01_2026-06-30_v2.xlsx');
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('falls back to the default filename without a filename* parameter', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: true,
      headers: new Headers({ 'Content-Disposition': 'attachment' }),
      blob: () => Promise.resolve(new Blob(['xlsx'])),
    } as Response);

    await downloadBrandReport('session-1', 'report-9');

    expect(clicked?.download).toBe('品牌分析报告.xlsx');
  });

  it('throws the server detail when the brand report export fails', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: 'report_not_found' }),
    } as Response);

    await expect(downloadBrandReport('session-1', 'report-9')).rejects.toThrow('report_not_found');
  });

  it('falls back to a generic error when the failure body is not json', async () => {
    vi.mocked(authorizedFetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('not json')),
    } as Response);

    await expect(downloadBrandReport('session-1', 'report-9')).rejects.toThrow('HTTP_500');
  });
});
