import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { KolSelectionPayload } from '../../api/agentArtifacts';
import KolSelectionArtifactView from './KolSelectionArtifactView';

const SCORE_DIMENSIONS = {
  industry_interest: { raw_score: 80, weight: 10, weighted_score: 8, source: 'audience_interests', missing_reason: null },
  target_region: { raw_score: 70, weight: 8, weighted_score: 5.6, source: 'audience_regions', missing_reason: null },
  target_age: { raw_score: 60, weight: 8, weighted_score: 4.8, source: 'audience_age', missing_reason: null },
  engagement: { raw_score: 90, weight: 20, weighted_score: 18, source: 'average_interactions', missing_reason: null },
  active_follower: { raw_score: 75, weight: 15, weighted_score: 11.25, source: 'effective_follower_rate', missing_reason: null },
  content: { raw_score: 85, weight: 15, weighted_score: 12.75, source: 'content_score', missing_reason: null },
  followers: { raw_score: 70, weight: 10, weighted_score: 7, source: 'followers', missing_reason: null },
  engagement_follower_ratio: { raw_score: 65, weight: 14, weighted_score: 9.1, source: 'interaction_follower_ratio', missing_reason: null },
};

function payload(): KolSelectionPayload {
  return {
    schema_version: 'kol_selection_v3',
    module: 'kol',
    data_status: 'complete',
    availability: {
      items: { status: 'complete', reason_codes: [] },
      scoring: { status: 'complete', reason_codes: [] },
      summary: { status: 'complete', reason_codes: [] },
    },
    limitations: [],
    methodology: { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] },
    scope: {
      brand: '海底捞',
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
        weights: { industry_interest: 10, target_region: 8, target_age: 8, engagement: 20, active_follower: 15, content: 15, followers: 10, engagement_follower_ratio: 14 },
        missing_value_policy: 'missing_as_zero',
      },
      items: [
        {
          rank: 1,
          platform: 'xiaohongshu',
          kol_uid: 'kol-1',
          nickname: '达人甲',
          avatar_url: null,
          homepage_url: null,
          followers: 120000,
          active_followers: 90000,
          active_follower_rate: 0.75,
          growth_rate: 0.1,
          engagement_total: 1000,
          avg_engagement: 200,
          likes: 500,
          comments: 200,
          shares: 100,
          quoted_price: 12000,
          reasons: ['高互动'],
          missing_fields: [],
          score_snapshot: { version: 'kol_score_v2', total: 85, rating: '重点推荐', stars: '★★★★★', data_completeness: 92, dimensions: SCORE_DIMENSIONS },
        },
        {
          rank: 2,
          platform: 'xiaohongshu',
          kol_uid: 'kol-2',
          nickname: '达人乙',
          avatar_url: null,
          homepage_url: null,
          followers: 80000,
          active_followers: 50000,
          active_follower_rate: 0.6,
          growth_rate: 0.05,
          engagement_total: 600,
          avg_engagement: 150,
          likes: 300,
          comments: 120,
          shares: 60,
          quoted_price: 9000,
          reasons: [],
          missing_fields: [],
          score_snapshot: { version: 'kol_score_v2', total: 72, rating: '推荐', stars: '★★★★', data_completeness: 100, dimensions: SCORE_DIMENSIONS },
        },
      ],
      summary: {
        candidate_count: 30,
        selected_count: 2,
        platform_distribution: [{ key: 'xiaohongshu', label: '小红书', count: 2, share: 1 }],
        rating_distribution: [
          { key: '重点推荐', label: '重点推荐', count: 1, share: 0.5 },
          { key: '推荐', label: '推荐', count: 1, share: 0.5 },
        ],
      },
    },
    narrative: { selection_summary: '基于互动表现与粉丝质量筛选', fit_findings: [], risk_notes: [], usage_advice: [] },
  };
}

describe('KolSelectionArtifactView', () => {
  it('v3 价值评分展示投放性价比指数，不为 v2 历史快照伪造价格分', () => {
    const base = payload();
    const valuePayload: KolSelectionPayload = {
      ...base,
      data: {
        ...base.data,
        scoring: {
          version: 'kol_value_score_v3',
          method: 'effect_plus_price_efficiency',
          weights: { effect_score: 70, price_efficiency_score: 30 },
          missing_value_policy: 'missing_as_zero',
        },
        items: base.data.items.map(item => ({
          ...item,
          score_snapshot: {
            version: 'kol_value_score_v3',
            effect_score: 62,
            price_efficiency_score: 21,
            value_score: 83,
            quoted_price: item.quoted_price,
            price_sample_size: 4,
            raw_price_efficiency: 0.005,
            price_efficiency_percentile: 0.7,
            rating: item.score_snapshot.rating,
            data_completeness: item.score_snapshot.data_completeness,
            dimensions: item.score_snapshot.dimensions,
          },
        })),
      },
    };
    render(<KolSelectionArtifactView payload={valuePayload} onOpenDetail={vi.fn()} />);

    expect(screen.getAllByText('投放性价比指数').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/效果与匹配度 70/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/价格效率 30/).length).toBeGreaterThan(0);
  });

  it('Top20 名单渲染，每张达人卡展示 score_snapshot 的 total/rating/stars/data_completeness', () => {
    render(<KolSelectionArtifactView payload={payload()} onOpenDetail={vi.fn()} />);

    const card = screen.getByText('达人甲').closest('section') as HTMLElement;
    expect(card).toBeTruthy();
    expect(within(card).getByText('85')).toBeVisible();
    expect(within(card).getByText('重点推荐')).toBeVisible();
    expect(within(card).getByText('★★★★★')).toBeVisible();
    expect(within(card).getByText(/数据完整度 92%/)).toBeVisible();
  });

  it('评分说明逐维展示 raw_score + weight + missing_reason，缺失维明确显示 0 分且不用 weighted_score 顶替', () => {
    const base = payload();
    const dims = {
      ...SCORE_DIMENSIONS,
      followers: { raw_score: 0, weight: 10, weighted_score: 0, source: null, missing_reason: 'missing_followers' },
    };
    const p: KolSelectionPayload = {
      ...base,
      data: {
        ...base.data,
        items: base.data.items.map((item, index) => (
          index === 0
            ? { ...item, score_snapshot: { ...item.score_snapshot, dimensions: dims, data_completeness: 82 } }
            : item
        )),
      },
    };
    render(<KolSelectionArtifactView payload={p} onOpenDetail={vi.fn()} />);

    expect(screen.getByText('评分说明')).toBeVisible();

    // 行业兴趣：raw_score 80、权重 10%。
    const industry = screen.getByTestId('score-dim-industry_interest');
    expect(within(industry).getByText('行业兴趣')).toBeVisible();
    expect(within(industry).getByText('80')).toBeVisible();
    expect(within(industry).getByText('10%')).toBeVisible();

    // 缺失维度：粉丝规模明确显示 0 分 + missing_reason。
    const followers = screen.getByTestId('score-dim-followers');
    expect(within(followers).getByText('粉丝规模')).toBeVisible();
    expect(within(followers).getByText('0分')).toBeVisible();
    expect(within(followers).getByText(/missing_followers/)).toBeVisible();

    // weighted_score（行业兴趣加权 8）不顶替原始分。
    expect(screen.queryByText('8')).not.toBeInTheDocument();
  });

  it('趋势现状视图渲染名单概览与分布', () => {
    render(<KolSelectionArtifactView payload={payload()} onOpenDetail={vi.fn()} />);

    expect(screen.getByText('趋势现状')).toBeVisible();
    expect(screen.getByText(/候选达人 30/)).toBeVisible();
    expect(screen.getByText(/已选 2/)).toBeVisible();
    expect(screen.getByText('小红书')).toBeVisible();
  });
});
