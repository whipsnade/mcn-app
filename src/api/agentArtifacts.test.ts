import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ApiAgentArtifact,
  ApiAgentArtifactVersion,
  AnalysisReportPayload,
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
  listArtifactReadStates,
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

function downloadResponse(filename: string): Response {
  return {
    ok: true,
    headers: {
      get: (name: string) => name.toLowerCase() === 'content-disposition'
        ? `attachment; filename="${filename}"`
        : null,
    },
    blob: async () => new Blob(['xlsx']),
  } as unknown as Response;
}

const emptyMethodology = {
  data_as_of: '2026-08-01T10:00:00',
  source_names: ['xiaohongshu'],
  notes: [],
};

const analysisReportFixture: AnalysisReportPayload = {
  schema_version: 'analysis_report_v1',
  module: 'report',
  data_status: 'complete',
  availability: {
    blocks: { status: 'complete', reason_codes: [] },
    fulfillment: { status: 'complete', reason_codes: [] },
  },
  limitations: [],
  methodology: emptyMethodology,
  title: '跨平台营销报告',
  subject_type: 'mixed',
  scope: { brand: '测试品牌', platforms: ['xiaohongshu'] },
  blocks: [{
    block_type: 'typed_table',
    id: 'platforms',
    title: '平台明细',
    columns: [
      { key: 'platform', label: '平台', type: 'string' },
      { key: 'volume', label: '声量', type: 'integer' },
    ],
    rows: [['小红书', 12]],
  }],
  fulfillment: [{
    key: 'requested_items',
    requested_min: 1,
    actual_count: 1,
    status: 'complete',
    reason: '返回全部结果',
  }],
  workbook: null,
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
    exclusions: ['竞品词'],
    official_accounts: [],
    comparison_mode: 'mom',
    attribution_rules: ['最后点击 7 天'],
    upload_ids: ['upload-1'],
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
    comparisons: {
      current_baseline: [
        { metric: 'volume', current: 500, baseline: 420, delta: 80, rate: 0.1905 },
      ],
      current_post: [],
    },
    attribution: { paid_confirmed: 30, organic: 8, unknown: 2, paid_confirmed_share: 0.75 },
    organic_summary: { volume: 160, engagement: 90, posts: 8, share_of_volume: null },
    audience_regions: [{ region: '上海', volume: 200, share: 0.4 }],
    internal_metrics: {
      spend: 100000,
      impressions: 2000000,
      conversions: 5000,
      revenue: 300000,
      cpc: 20,
      cpm: 50,
    },
    roi: { spend: 100000, revenue: 300000, conversions: 5000, attribution_window: '最后点击 7 天', roi: 2, roas: 3 },
  },
  narrative: { executive_summary: '活动总结', phase_review: [], findings: [], recommendations: [] },
};

// Golden fixture：活动 ROI 可选章节为 null（数据不足时后端输出 None）。
const campaignRoiNullFixture: CampaignReportPayload = {
  schema_version: 'campaign_report_v2',
  module: 'campaign',
  data_status: 'restricted',
  availability: { overview: { status: 'complete', reason_codes: [] } },
  limitations: [{ code: 'roi_missing', message: 'ROI 数据不足', affected_paths: ['roi'] }],
  methodology: emptyMethodology,
  scope: {
    brand: '测试品牌',
    campaign: '无 ROI 活动',
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
    comparisons: { current_baseline: [], current_post: [] },
    attribution: null,
    organic_summary: null,
    audience_regions: [],
    internal_metrics: null,
    roi: null,
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
      audience: { regions: ['上海'], age_ranges: ['18-24'], interests: ['美食'] },
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

const v3Dimension = (rawScore: number, weight: number) => ({
  raw_score: rawScore,
  weight,
  weighted_score: Math.round(rawScore * weight) / 100,
  source: null,
  missing_reason: null,
});

// Golden fixture：kol_value_score_v3 判别联合分支（Gate C Task 1/2 新契约）。
const kolValueV3Fixture: KolSelectionPayload = {
  schema_version: 'kol_selection_v3',
  module: 'kol',
  data_status: 'complete',
  availability: { items: { status: 'complete', reason_codes: [] } },
  limitations: [],
  methodology: emptyMethodology,
  scope: {
    brand: null,
    category: '美食',
    campaign: null,
    platforms: ['xiaohongshu'],
    audience: { regions: ['上海'], age_ranges: [], interests: [] },
    filters: { budget_min: null, budget_max: null, follower_min: null, follower_max: null },
    content_formats: ['视频'],
  },
  data: {
    scoring: {
      version: 'kol_value_score_v3',
      method: 'effect_plus_price_efficiency',
      weights: { average_interactions: 14 },
      missing_value_policy: 'missing_as_zero',
    },
    items: [{
      rank: 1,
      platform: 'xiaohongshu',
      kol_uid: 'kol-1',
      nickname: '达人甲',
      avatar_url: null,
      homepage_url: null,
      followers: 500000,
      active_followers: 300000,
      active_follower_rate: 60,
      growth_rate: 0.3,
      engagement_total: 20000,
      avg_engagement: 20,
      likes: 50,
      comments: 30,
      shares: 20,
      quoted_price: 800,
      reasons: [],
      missing_fields: [],
      audience: { regions: ['上海'], age_ranges: ['18-24'], interests: ['美食'] },
      score_snapshot: {
        version: 'kol_value_score_v3',
        effect_score: 52.3,
        price_efficiency_score: 18.6,
        value_score: 70.9,
        quoted_price: 800,
        price_sample_size: 5,
        raw_price_efficiency: 0.62,
        price_efficiency_percentile: 62,
        rating: '推荐',
        data_completeness: 0.9,
        dimensions: {
          average_interactions: v3Dimension(80, 14),
          active_follower: v3Dimension(70, 10),
          engagement_follower_ratio: v3Dimension(75, 10),
          content_match: v3Dimension(90, 10),
          followers: v3Dimension(60, 7),
          industry_interest: v3Dimension(85, 7),
          target_region: v3Dimension(50, 6),
          target_age: v3Dimension(40, 6),
        },
      },
    }],
    summary: { candidate_count: 20, selected_count: 1, platform_distribution: [], rating_distribution: [] },
  },
  narrative: { selection_summary: 'v3 圈选说明', fit_findings: [], risk_notes: [], usage_advice: [] },
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

  it('lists server-side artifact read states for session init', async () => {
    const { request } = await import('./client');
    vi.mocked(request).mockResolvedValue([{ module: 'brand', last_seen_sequence: 12 }]);

    const result = await listArtifactReadStates('s1');

    expect(request).toHaveBeenCalledWith('/api/v1/agent/sessions/s1/artifact-read-states');
    expect(result).toEqual([{ module: 'brand', last_seen_sequence: 12 }]);
  });

  it('exports an artifact as a downloaded xlsx blob', async () => {
    const { authorizedFetch } = await import('./client');
    vi.mocked(authorizedFetch).mockResolvedValue(downloadResponse('brand_report_v3.xlsx'));
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

  it('exports the explicitly viewed version via the version query param', async () => {
    const { authorizedFetch } = await import('./client');
    vi.mocked(authorizedFetch).mockResolvedValue(downloadResponse('brand_report_v3_v1.xlsx'));
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock-download'),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    await exportArtifact('art-1', 1);

    expect(authorizedFetch).toHaveBeenCalledWith('/api/v1/agent/artifacts/art-1/export?version=1');
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
      campaignRoiNullFixture,
      kolSelectionFixture,
      kolValueV3Fixture,
      kolAnalysisFixture,
      kolDetailFixture,
      insightBoardFixture,
      analysisReportFixture,
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
          expect(fixture.scope.campaign).toBeTypeOf('string');
          break;
        case 'kol_selection_v3':
          // 循环断言只做版本无关检查；判别分支的精确断言在
          // kol_value_score_v3 / kol_score_v2 两个专项测试里。
          expect(fixture.data.items).toBeInstanceOf(Array);
          expect(fixture.data.summary.selected_count).toBe(1);
          expect(fixture.data.items[0].audience.regions).toBeInstanceOf(Array);
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
        case 'analysis_report_v1':
          expect(fixture.blocks[0]?.block_type).toBe('typed_table');
          expect(fixture.fulfillment[0]?.actual_count).toBe(1);
          break;
        default:
          break;
      }
    }
  });

  it('parses kol_value_score_v3 snapshots with effect/price/completeness/dimensions', () => {
    expect(isAgentArtifactPayload(kolValueV3Fixture)).toBe(true);
    if (kolValueV3Fixture.schema_version !== 'kol_selection_v3') throw new Error('unreachable');
    expect(kolValueV3Fixture.data.scoring.version).toBe('kol_value_score_v3');
    expect(kolValueV3Fixture.data.scoring.method).toBe('effect_plus_price_efficiency');
    // 本次圈选确认的内容形式（决定图文/视频报价选择）：新契约必填、旧版可缺失。
    expect(kolValueV3Fixture.scope.content_formats).toEqual(['视频']);

    const snapshot = kolValueV3Fixture.data.items[0].score_snapshot;
    expect(snapshot.version).toBe('kol_value_score_v3');
    if (snapshot.version === 'kol_value_score_v3') {
      expect(snapshot.effect_score).toBe(52.3);
      expect(snapshot.price_efficiency_score).toBe(18.6);
      expect(snapshot.value_score).toBe(70.9);
      expect(snapshot.quoted_price).toBe(800);
      expect(snapshot.price_sample_size).toBe(5);
      expect(snapshot.rating).toBe('推荐');
      expect(snapshot.data_completeness).toBe(0.9);
      expect(snapshot.dimensions.average_interactions.raw_score).toBe(80);
      expect(snapshot.dimensions.target_age.weight).toBe(6);
    }
  });

  it('keeps legacy kol_score_v2 snapshots readable', () => {
    expect(isAgentArtifactPayload(kolSelectionFixture)).toBe(true);
    if (kolSelectionFixture.schema_version !== 'kol_selection_v3') throw new Error('unreachable');
    // 旧 Version 的 scope 无 content_formats：可选字段必须仍可读取（undefined 兜底）。
    expect(kolSelectionFixture.scope.content_formats).toBeUndefined();
    const snapshot = kolSelectionFixture.data.items[0].score_snapshot;
    expect(snapshot.version).toBe('kol_score_v2');
    if (snapshot.version === 'kol_score_v2') {
      expect(snapshot.total).toBe(85);
      expect(snapshot.rating).toBe('S');
      expect(snapshot.stars).toBe('5');
      expect(snapshot.dimensions.fans.weighted_score).toBeCloseTo(13.5);
    }
  });

  it('reads campaign comparisons/attribution/organic/audience/internal/roi sections', () => {
    if (campaignFixture.schema_version !== 'campaign_report_v2') throw new Error('unreachable');
    expect(campaignFixture.scope.attribution_rules).toEqual(['最后点击 7 天']);
    expect(campaignFixture.scope.comparison_mode).toBe('mom');
    expect(campaignFixture.scope.upload_ids).toEqual(['upload-1']);
    expect(campaignFixture.data.comparisons.current_baseline[0]).toEqual({
      metric: 'volume', current: 500, baseline: 420, delta: 80, rate: 0.1905,
    });
    expect(campaignFixture.data.attribution?.paid_confirmed).toBe(30);
    expect(campaignFixture.data.organic_summary?.posts).toBe(8);
    expect(campaignFixture.data.audience_regions[0]).toEqual({ region: '上海', volume: 200, share: 0.4 });
    expect(campaignFixture.data.internal_metrics?.cpm).toBe(50);
    expect(campaignFixture.data.roi?.roas).toBe(3);
  });

  it('parses campaign payloads with roi null (data insufficient)', () => {
    if (campaignRoiNullFixture.schema_version !== 'campaign_report_v2') throw new Error('unreachable');
    expect(campaignRoiNullFixture.data.roi).toBeNull();
    expect(campaignRoiNullFixture.data.internal_metrics).toBeNull();
    expect(campaignRoiNullFixture.data.organic_summary).toBeNull();
    expect(campaignRoiNullFixture.data.attribution).toBeNull();
    expect(campaignRoiNullFixture.data_status).toBe('restricted');
  });
});
