import type { ApiBiReport, ApiCandidatePage, BiAnalyticsData } from '../api/contracts';

export const candidatePage: ApiCandidatePage = {
  task_id: 'task-1',
  version: 2,
  total: 2,
  items: [
    {
      id: 'candidate-a', kol_id: 'kol-a', platform: 'xiaohongshu', platform_account_id: 'xhs-a',
      nickname: '达人甲', profile_url: 'https://example.test/a', rank: 1, total_score: 88,
      scores: { audience: 92, engagement: 76, budget: 81, content: 89, platform: 85, risk: 90 },
      matched_conditions: ['美妆垂类', '一线城市受众'], risks: [], recommendation: '优先联系',
      metrics: { followers: 120_000, quoted_price_cny: 6_000, collected_at: '2026-07-15T10:00:00Z', data_completeness: 92 },
    },
    {
      id: 'candidate-b', kol_id: 'kol-b', platform: 'douyin', platform_account_id: 'dy-b',
      nickname: '达人乙', profile_url: 'https://example.test/b', rank: 2, total_score: 84,
      scores: { audience: 80, engagement: 94, budget: 76, content: 88, platform: 79, risk: 83 },
      matched_conditions: ['短视频种草', '高互动率'], risks: [{ level: 'low', label: '报价波动' }], recommendation: '建议沟通档期',
      metrics: { followers: 900_000, quoted_price_cny: 19_000, collected_at: '2026-07-14T10:00:00Z', data_completeness: 84 },
    },
  ],
};

export const candidate = candidatePage.items[0];

export const emptyAnalytics: BiAnalyticsData = {
  overview: {
    brand_volume: { value: null, unit: '条', available: false, coverage: 0, source_fields: [], platforms: [] },
    total_exposure: { value: null, unit: '次', available: false, coverage: 0, source_fields: [], platforms: [] },
    average_engagement_rate: { value: null, unit: '%', available: false, coverage: 0, source_fields: [], platforms: [] },
  },
  sentiment: { available: false, coverage: 0, source_fields: [], platforms: [], items: [], hot_words: [] },
  exposure_trend: [],
  audience: {
    age: { value: null, unit: '%', available: false, coverage: 0, source_fields: [], platforms: [], items: [] },
    gender: { value: null, unit: '%', available: false, coverage: 0, source_fields: [], platforms: [], items: [] },
    regions: { value: null, unit: '%', available: false, coverage: 0, source_fields: [], platforms: [], items: [] },
  },
};

export function reportFixture(overrides: Partial<ApiBiReport> = {}): ApiBiReport {
  return {
    id: 'report-2', task_id: 'task-1', report_version: 1, candidate_version: 2,
    overview: { total_candidates: 2 }, score_composition: [], audience_content_fit: {},
    platform_distribution: [], budget_analysis: {}, comparison: [], risks: [], analytics: emptyAnalytics,
    conclusion: '优先联系达人甲。', sources: [], generated_at: '2026-07-15T10:00:00Z',
    ...overrides,
  };
}

export const missingDataReport = reportFixture({
  overview: {},
  conclusion: '部分候选的公开指标尚待补充。',
});
