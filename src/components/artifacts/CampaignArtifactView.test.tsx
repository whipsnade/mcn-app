import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { CampaignReportPayload } from '../../api/agentArtifacts';
import CampaignArtifactView from './CampaignArtifactView';

function campaignPayload(): CampaignReportPayload {
  return {
    schema_version: 'campaign_report_v2',
    module: 'campaign',
    data_status: 'complete',
    availability: {},
    limitations: [],
    methodology: { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] },
    scope: {
      brand: '海底捞',
      campaign: '新品上市',
      period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
      platforms: ['xiaohongshu'],
      keywords: [],
    },
    data: {
      overview: {
        total_volume: 12000,
        total_engagement: 340,
        total_posts: 12,
        total_creators: 4,
        sentiment_score: 0.66,
      },
      platform_contributions: [],
      timeline: [],
      kol_contributions: [],
      content_types: [],
      sentiment: {
        summary: {
          positive: { count: 6, share: 0.6 },
          neutral: { count: 3, share: 0.3 },
          negative: { count: 1, share: 0.1 },
        },
        by_platform: [],
      },
      top_posts: [{
        platform: 'xiaohongshu',
        post_id: 'p1',
        title: '新品试吃活动',
        url: 'https://xhs.com/p1',
        author: '美食家',
        published_at: '2026-07-15',
        likes: 500,
        comments: 60,
        shares: 20,
        engagement: 580,
      }],
    },
    narrative: {
      executive_summary: '活动整体表现良好。',
      phase_review: [],
      findings: [],
      recommendations: [],
    },
  };
}

describe('CampaignArtifactView', () => {
  it('http/https 热帖渲染为可点链接', () => {
    render(<CampaignArtifactView payload={campaignPayload()} />);

    expect(screen.getByRole('link', { name: '新品试吃活动' })).toHaveAttribute('href', 'https://xhs.com/p1');
  });

  it('非 http/https 的热帖链接降级为不可点文本（URL 白名单）', () => {
    const base = campaignPayload();
    const p: CampaignReportPayload = {
      ...base,
      data: {
        ...base.data,
        top_posts: [{ ...base.data.top_posts[0], url: 'javascript:alert(1)' }],
      },
    };
    render(<CampaignArtifactView payload={p} />);

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('新品试吃活动')).toBeVisible();
    expect(screen.getByText(/数据受限（无原文链接）/)).toBeVisible();
  });
});
