import { afterEach, describe, expect, it, vi } from 'vitest';

import { getArtifactsSummary, listSessionReports, markArtifactRead } from './reports';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

describe('reports api', () => {
  afterEach(() => {
    vi.clearAllMocks();
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
});
