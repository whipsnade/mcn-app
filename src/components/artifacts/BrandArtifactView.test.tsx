import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { BrandReportPayload } from '../../api/agentArtifacts';
import BrandArtifactView from './BrandArtifactView';

function brandPayload(): BrandReportPayload {
  return {
    schema_version: 'brand_report_v3',
    module: 'brand',
    data_status: 'complete',
    availability: {},
    limitations: [],
    methodology: { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] },
    scope: {
      brand: '海底捞',
      period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
      platforms: ['xiaohongshu', 'douyin'],
      keywords: ['排队'],
      comparison_mode: 'mom',
    },
    data: {
      overview: {
        total_volume: 120000,
        total_engagement: 3400,
        total_posts: 86,
        sentiment_score: 0.72,
        platforms: [
          { platform: 'xiaohongshu', volume: 80000, engagement: 2000, posts: 50, share_of_voice: 0.67, sentiment_score: 0.75 },
          { platform: 'douyin', volume: 40000, engagement: 1400, posts: 36, share_of_voice: 0.33, sentiment_score: 0.68 },
        ],
      },
      comparisons: {
        mom: {
          status: 'available',
          baseline_period: { start: '2026-06-01', end: '2026-06-30', timezone: 'Asia/Shanghai' },
          metrics: [{ metric: 'total_volume', current: 120000, baseline: 108000, delta: 12000, rate: 0.111 }],
        },
        yoy: { status: 'not_requested', baseline_period: null, metrics: [] },
      },
      sentiment: {
        summary: {
          positive: { count: 60, share: 0.6 },
          neutral: { count: 20, share: 0.2 },
          negative: { count: 20, share: 0.2 },
        },
        by_platform: [
          { platform: 'xiaohongshu', positive: { count: 40, share: 0.5 }, neutral: { count: 12, share: 0.15 }, negative: { count: 12, share: 0.15 } },
        ],
      },
      daily_trend: [
        { date: '2026-07-01', platform: 'xiaohongshu', volume: 4000, engagement: 120, positive: 10, neutral: 5, negative: 2 },
        { date: '2026-07-02', platform: 'xiaohongshu', volume: 4500, engagement: 130, positive: 11, neutral: 5, negative: 1 },
      ],
      content_types: [
        { platform: 'xiaohongshu', type: '图文', posts: 40, volume: 60000, engagement: 1500 },
        { platform: 'xiaohongshu', type: '视频', posts: 10, volume: 20000, engagement: 500 },
      ],
      creator_tiers: [
        { platform: 'xiaohongshu', tier: '头部', creator_count: 3, posts: 10, volume: 30000, engagement: 900 },
      ],
      organic_vs_paid: [
        { platform: 'xiaohongshu', kind: 'organic', posts: 45, volume: 70000, engagement: 1800 },
        { platform: 'xiaohongshu', kind: 'paid', posts: 5, volume: 10000, engagement: 200 },
      ],
      regions: [
        { region: '上海', volume: 40000, share: 0.33, sentiment_score: 0.75 },
        { region: '北京', volume: 30000, share: 0.25, sentiment_score: 0.7 },
      ],
      topics: [
        { topic: '排队', volume: 30000, engagement: 1200, sentiment_score: 0.4 },
        { topic: '新品', volume: 25000, engagement: 800, sentiment_score: 0.8 },
      ],
      top_posts: [{
        platform: 'xiaohongshu',
        post_id: 'p1',
        title: '海底捞隐藏吃法合集',
        url: 'https://xhs.com/p1',
        author: '美食家',
        published_at: '2026-07-15',
        likes: 5000,
        comments: 300,
        shares: 100,
        engagement: 5400,
      }],
    },
    narrative: {
      executive_summary: '品牌整体声量健康，负面集中于排队体验。',
      findings: [{ title: '负面集中于排队', detail: '排队相关话题负面占比偏高', supporting_paths: ['data.topics.0'] }],
      recommendations: [{ title: '优化排队体验', action: '上线预约取号', rationale: '降低负面情绪', supporting_paths: [] }],
    },
  };
}

describe('BrandArtifactView', () => {
  it('brand_report_v3 从数据渲染八个章节', () => {
    render(<BrandArtifactView payload={brandPayload()} />);

    for (const title of ['概览', '情感分析', '声量趋势', '内容类型', '创作者分层', '地域分布', '话题洞察', '热帖']) {
      expect(screen.getByText(title)).toBeVisible();
    }

    expect(screen.getByText('海底捞')).toBeVisible();
    expect(screen.getByText('总声量')).toBeVisible();
    expect(screen.getByText('12万')).toBeVisible();
    expect(screen.getByText('86')).toBeVisible();
    expect(screen.getAllByText('正面').length).toBeGreaterThan(0);
    expect(screen.getByText('头部')).toBeVisible();
    expect(screen.getByText('上海')).toBeVisible();
    expect(screen.getByText('排队')).toBeVisible();
    expect(screen.getByText('海底捞隐藏吃法合集')).toBeVisible();
  });

  it('null 数值显示「数据受限」，绝不显示 0', () => {
    const base = brandPayload();
    const p: BrandReportPayload = {
      ...base,
      data: {
        ...base.data,
        overview: {
          total_volume: null,
          total_engagement: null,
          total_posts: null,
          sentiment_score: null,
          platforms: [],
        },
      },
    };
    const { container } = render(<BrandArtifactView payload={p} />);

    const overview = container.querySelector('[data-chapter="overview"]') as HTMLElement;
    expect(overview).toBeTruthy();
    expect(within(overview).getAllByText('数据受限').length).toBeGreaterThanOrEqual(4);
    expect(within(overview).queryByText('0')).not.toBeInTheDocument();
  });

  it('restricted data_status 展示限制说明与受限徽标', () => {
    const base = brandPayload();
    const p: BrandReportPayload = {
      ...base,
      data_status: 'restricted',
      limitations: [{ code: 'no_data', message: '部分平台未采集到数据', affected_paths: ['data.regions'] }],
    };
    render(<BrandArtifactView payload={p} />);

    expect(screen.getByText('数据受限')).toBeVisible();
    expect(screen.getByText('部分平台未采集到数据')).toBeVisible();
  });

  it('渲染叙事章节（执行摘要 / 发现 / 建议）', () => {
    render(<BrandArtifactView payload={brandPayload()} />);

    expect(screen.getByText('执行摘要')).toBeVisible();
    expect(screen.getByText('品牌整体声量健康，负面集中于排队体验。')).toBeVisible();
    expect(screen.getByText('发现')).toBeVisible();
    expect(screen.getByText('负面集中于排队')).toBeVisible();
    expect(screen.getByText('建议')).toBeVisible();
    expect(screen.getByText('优化排队体验')).toBeVisible();
  });

  it('daily_trend 中 null 声量渲染为数据受限缺口而非 0', () => {
    const base = brandPayload();
    const p: BrandReportPayload = {
      ...base,
      data: {
        ...base.data,
        daily_trend: [
          { date: '2026-07-01', platform: 'xiaohongshu', volume: 4000, engagement: 120, positive: 10, neutral: 5, negative: 2 },
          { date: '2026-07-02', platform: 'xiaohongshu', volume: null, engagement: null, positive: 11, neutral: 5, negative: 1 },
        ],
      },
    };
    const { container } = render(<BrandArtifactView payload={p} />);
    const trend = container.querySelector('[data-chapter="daily_trend"]') as HTMLElement;
    expect(trend).toBeTruthy();

    // 受限日期被明确披露为「数据受限」，而不是把 null 当 0 渲染。
    expect(within(trend).getByText(/07-02 数据受限/)).toBeVisible();
    // 正常日期不被标记受限。
    expect(within(trend).queryByText(/07-01 数据受限/)).not.toBeInTheDocument();
  });
});
