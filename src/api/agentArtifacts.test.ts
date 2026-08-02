import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ApiAgentArtifact,
  ApiAgentArtifactVersion,
  BrandReportPayload,
  CampaignReportPayload,
  InsightBoardPayload,
  KolAnalysisPayload,
  KolDetailPayload,
  KolSelectionPayload,
} from './agentArtifacts';
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

const emptyMethodology = {
  data_as_of: '2026-08-01T10:00:00',
  source_names: ['xiaohongshu'],
  notes: [],
};

// Golden fixtures：每个 schema_version 一份后端形状的强类型 JSON。
// DTO 字段名与后端漂移时，此处编译期 + 运行期断言都会大声失败。
const brandFixture: BrandReportPayload = {
  schema_version: 'brand_report_v3',
  module: 'brand',
  data_status: 'complete',
  availability: { overview: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: {
    brand: '测试品牌',
    period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
    platforms: ['xiaohongshu'],
    keywords: [],
    comparison_mode: 'none',
  },
  data: {
    overview: { total_volume: 1234, total_engagement: 99, total_posts: 88, sentiment_score: 0.7, platforms: [] },
    comparisons: {
      mom: { status: 'not_requested', baseline_period: null, metrics: [] },
      yoy: { status: 'not_requested', baseline_period: null, metrics: [] },
    },
    sentiment: {
      summary: {
        positive: { count: 10, share: 0.5 },
        neutral: { count: 5, share: 0.25 },
        negative: { count: 5, share: 0.25 },
      },
      by_platform: [],
    },
    daily_trend: [],
    content_types: [],
    creator_tiers: [],
    organic_vs_paid: [],
    regions: [],
    topics: [],
    top_posts: [],
  },
  narrative: { executive_summary: '总结', findings: [], recommendations: [] },
};

const campaignFixture: CampaignReportPayload = {
  schema_version: 'campaign_report_v2',
  module: 'campaign',
  data_status: 'complete',
  availability: { overview: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: {
    brand: '测试品牌',
    campaign: '夏季活动',
    period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
    platforms: ['xiaohongshu'],
    keywords: [],
  },
  data: {
    overview: { total_volume: 500, total_engagement: 200, total_posts: 40, total_creators: 5, sentiment_score: 0.6 },
    platform_contributions: [],
    timeline: [],
    kol_contributions: [],
    content_types: [],
    sentiment: {
      summary: {
        positive: { count: 30, share: 0.6 },
        neutral: { count: 10, share: 0.2 },
        negative: { count: 10, share: 0.2 },
      },
      by_platform: [],
    },
    top_posts: [],
  },
  narrative: { executive_summary: '活动总结', phase_review: [], findings: [], recommendations: [] },
};

const kolSelectionFixture: KolSelectionPayload = {
  schema_version: 'kol_selection_v3',
  module: 'kol',
  data_status: 'complete',
  availability: { items: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: {
    brand: '测试品牌',
    category: null,
    campaign: null,
    platforms: ['xiaohongshu'],
    audience: { regions: [], age_ranges: [], interests: [] },
    filters: { budget_min: null, budget_max: null, follower_min: null, follower_max: null },
  },
  data: {
    scoring: {
      version: 'kol_score_v2',
      method: 'weighted_sum',
      weights: { fans: 15 },
      missing_value_policy: 'missing_as_zero',
    },
    items: [{
      rank: 1,
      platform: 'xiaohongshu',
      kol_uid: 'kol-1',
      nickname: '达人甲',
      avatar_url: null,
      homepage_url: null,
      followers: 1000,
      active_followers: 800,
      active_follower_rate: 0.8,
      growth_rate: 0.1,
      engagement_total: 100,
      avg_engagement: 10,
      likes: 50,
      comments: 20,
      shares: 5,
      quoted_price: 100,
      reasons: [],
      missing_fields: [],
      score_snapshot: {
        version: 'kol_score_v2',
        total: 85,
        rating: 'S',
        stars: '5',
        data_completeness: 1,
        dimensions: {
          fans: { raw_score: 90, weight: 15, weighted_score: 13.5, source: null, missing_reason: null },
        },
      },
    }],
    summary: { candidate_count: 10, selected_count: 1, platform_distribution: [], rating_distribution: [] },
  },
  narrative: { selection_summary: '圈选说明', fit_findings: [], risk_notes: [], usage_advice: [] },
};

const kolAnalysisFixture: KolAnalysisPayload = {
  schema_version: 'kol_analysis_v2',
  module: 'kol',
  data_status: 'complete',
  availability: { summary: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: { selection_artifact_id: 'art-sel', selection_version: '1', analysis_period: null },
  data: {
    summary: { kol_count: 5, total_followers: 1000, total_active_followers: 800, total_engagement: 100, avg_score: 80 },
    platform_distribution: [],
    rating_distribution: [],
    follower_distribution: [],
    engagement_distribution: [],
    region_distribution: [],
    kol_trend: [{
      platform: 'xiaohongshu',
      kol_uid: 'kol-1',
      nickname: '达人甲',
      followers: 1000,
      active_followers: 800,
      engagement_total: 100,
      avg_engagement: 10,
      growth_rate: 0.1,
      score: 80,
    }],
    top_kols: [{ rank: 1, platform: 'xiaohongshu', kol_uid: 'kol-1', nickname: '达人甲', score: 80, engagement_total: 100, rating: 'S' }],
  },
  narrative: { executive_summary: '分析总结', portfolio_findings: [], mix_recommendations: [], risk_notes: [] },
};

const kolDetailFixture: KolDetailPayload = {
  schema_version: 'kol_detail_v2',
  module: 'kol',
  data_status: 'complete',
  availability: { identity: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: { platform: 'xiaohongshu', kol_uid: 'kol-1', selection_artifact_id: null, selection_version: null },
  data: {
    identity: { nickname: '达人甲', avatar_url: null, homepage_url: null, bio: '美妆博主', verification: true, region: '上海' },
    metrics: { followers: 1000, following: 10, posts: 20, likes: 100, active_followers: 800, active_follower_rate: 0.8, growth_rate: 0.1, engagement_total: 100, avg_engagement: 10 },
    audience: { gender_distribution: [], age_distribution: [], region_distribution: [], interest_distribution: [] },
    trend: [{ date: '2026-07-01', followers: 1000, engagement: 100, posts: 2 }],
    latest_posts: [],
    cache: { hit: true, fetched_at: '2026-08-01T10:00:00', expires_at: '2026-08-02T10:00:00' },
  },
  narrative: { profile_summary: '达人概览', content_strengths: [], commercial_notes: [], risk_notes: [] },
};

const insightBoardFixture: InsightBoardPayload = {
  schema_version: 'insight_board_v1',
  title: '竞品钻取',
  data_status: 'complete',
  availability: { blocks: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: { summary: '', period: null, platforms: [], brand: '竞品', campaign: null, kol_uid: null },
  parent_artifact_id: 'parent-1',
  narrative: { summary: '钻取说明', findings: [] },
  data: [{ block_type: 'metric_grid', title: '核心指标', cards: [{ key: 'volume', label: '声量', value: 120 }] }],
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

  it('validates golden backend-shaped fixtures for every schema_version with full typing reachable', () => {
    const fixtures = [
      brandFixture,
      campaignFixture,
      kolSelectionFixture,
      kolAnalysisFixture,
      kolDetailFixture,
      insightBoardFixture,
    ];

    // 每个后端形状的 fixture 都必须通过判别联合守卫。
    for (const fixture of fixtures) {
      expect(isAgentArtifactPayload(fixture), fixture.schema_version).toBe(true);
    }

    // 收窄后 data 字段必须可达且值真实（后端改字段名 → 断言大声失败）。
    for (const fixture of fixtures) {
      switch (fixture.schema_version) {
        case 'brand_report_v3':
          expect(fixture.data.overview.total_volume).toBe(1234);
          expect(fixture.data.topics).toEqual([]);
          expect(fixture.scope.brand).toBe('测试品牌');
          break;
        case 'campaign_report_v2':
          expect(fixture.data.overview.total_creators).toBe(5);
          expect(fixture.scope.campaign).toBe('夏季活动');
          break;
        case 'kol_selection_v3':
          expect(fixture.data.scoring.version).toBe('kol_score_v2');
          expect(fixture.data.items[0].score_snapshot.dimensions.fans.weighted_score).toBeCloseTo(13.5);
          expect(fixture.data.summary.selected_count).toBe(1);
          break;
        case 'kol_analysis_v2':
          expect(fixture.data.summary.kol_count).toBe(5);
          expect(fixture.data.top_kols[0].rating).toBe('S');
          break;
        case 'kol_detail_v2':
          expect(fixture.data.identity.verification).toBe(true);
          expect(fixture.data.cache.hit).toBe(true);
          expect(fixture.data.metrics.followers).toBe(1000);
          break;
        case 'insight_board_v1':
          expect(fixture.data[0].block_type).toBe('metric_grid');
          expect(fixture.parent_artifact_id).toBe('parent-1');
          break;
        default:
          break;
      }
    }
  });
});
