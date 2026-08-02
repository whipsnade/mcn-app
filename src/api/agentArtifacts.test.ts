import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentArtifact, ApiAgentArtifactVersion } from './agentArtifacts';
import {
  exportArtifact,
  getArtifact,
  getArtifactVersion,
  isAgentArtifactPayload,
  listArtifacts,
  markArtifactRead,
} from './agentArtifacts';

vi.mock('./client', () => ({
  authorizedFetch: vi.fn(),
  request: vi.fn(),
}));

const artifact: ApiAgentArtifact = {
  id: 'art-1',
  module: 'brand',
  artifact_type: 'brand_report_v3',
  parent_artifact_id: null,
  artifact_key: 'brand_report',
  status: 'published',
  latest_version: 3,
  activity_sequence: 9,
  created_at: '2026-08-01T10:00:00',
  updated_at: '2026-08-01T10:00:00',
};

const brandVersion: ApiAgentArtifactVersion = {
  id: 'ver-3',
  artifact_id: 'art-1',
  version: 3,
  schema_version: 'brand_report_v3',
  data_status: 'complete',
  payload: {
    schema_version: 'brand_report_v3',
    module: 'brand',
    data_status: 'complete',
    availability: { overview: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology: {
      data_as_of: '2026-08-01T10:00:00',
      source_names: ['xiaohongshu'],
      notes: [],
    },
    scope: {
      brand: '测试品牌',
      period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
      platforms: ['xiaohongshu'],
      keywords: [],
      comparison_mode: 'none',
    },
    data: {
      overview: {
        total_volume: 1234,
        total_engagement: 99,
        total_posts: 88,
        sentiment_score: 0.7,
        platforms: [],
      },
    },
    narrative: {
      executive_summary: '总结',
      findings: [],
      recommendations: [],
    },
  },
  evidence_refs: [],
  created_at: '2026-08-01T10:00:00',
};

describe('agent artifacts api', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it('lists artifacts with module and parent filters', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue([artifact]);

    await listArtifacts('s1');
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/artifacts');

    await listArtifacts('s1', 'brand', 'parent-1');
    expect(request).toHaveBeenCalledWith(
      '/api/v1/agent/sessions/s1/artifacts?module=brand&parent_artifact_id=parent-1',
    );
  });

  it('gets an artifact and a specific version', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValueOnce(artifact);
    vi.mocked(request).mockResolvedValueOnce(brandVersion);

    await expect(getArtifact('art-1')).resolves.toEqual(artifact);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/artifacts/art-1');

    await expect(getArtifactVersion('art-1', 3)).resolves.toEqual(brandVersion);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/artifacts/art-1/versions/3');
  });

  it('marks a module read at the last seen sequence', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue({ module: 'brand', last_seen_sequence: 12 });

    const result = await markArtifactRead('s1', 'brand', 12);
    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/artifact-read-state', {
      method: 'PUT',
      body: JSON.stringify({ module: 'brand', last_seen_sequence: 12 }),
    });
    expect(result.last_seen_sequence).toBe(12);
  });

  it('exports an artifact as a downloaded xlsx blob', async () => {
    const { authorizedFetch } = await import('./client');
    vi.mocked(authorizedFetch).mockResolvedValue(new Response(new Blob(['xlsx']), {
      status: 200,
      headers: { 'Content-Disposition': 'attachment; filename="brand_report_v3.xlsx"' },
    }));
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock-download'),
      revokeObjectURL: vi.fn(),
    }));
    const clicked: Array<{ href: string; download: string }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.href, download: this.download });
    });

    await exportArtifact('art-1');

    expect(authorizedFetch).toHaveBeenCalledWith('/api/v1/agent/artifacts/art-1/export');
    expect(clicked).toEqual([{ href: 'blob:mock-download', download: 'brand_report_v3.xlsx' }]);
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('narrows the payload union by schema_version with the type guard', () => {
    const payload: unknown = brandVersion.payload;
    expect(isAgentArtifactPayload(payload)).toBe(true);

    if (isAgentArtifactPayload(payload)) {
      switch (payload.schema_version) {
        case 'brand_report_v3':
          // data is strongly typed, not Record<string, unknown>
          expect(payload.data.overview.total_volume).toBe(1234);
          expect(payload.data.overview.platforms).toEqual([]);
          expect(payload.module).toBe('brand');
          expect(payload.scope.brand).toBe('测试品牌');
          break;
        case 'campaign_report_v2':
          expect(payload.data.overview.total_posts).toBeTypeOf('number');
          break;
        case 'kol_selection_v3':
          expect(payload.data.items).toBeInstanceOf(Array);
          break;
        case 'kol_analysis_v2':
          expect(payload.data.top_kols).toBeInstanceOf(Array);
          break;
        case 'kol_detail_v2':
          expect(payload.data.identity.nickname).toBeTypeOf('string');
          break;
        case 'insight_board_v1':
          expect(payload.data).toBeInstanceOf(Array);
          break;
        default:
          break;
      }
    }

    expect(isAgentArtifactPayload({ schema_version: 'future_v9' })).toBe(false);
    expect(isAgentArtifactPayload(null)).toBe(false);
    expect(isAgentArtifactPayload('brand_report_v3')).toBe(false);
  });
});
