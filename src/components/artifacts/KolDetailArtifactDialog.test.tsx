import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { KolDetailPayload } from '../../api/agentArtifacts';
import KolDetailArtifactDialog from './KolDetailArtifactDialog';

function detail(): KolDetailPayload {
  return {
    schema_version: 'kol_detail_v2',
    module: 'kol',
    data_status: 'complete',
    availability: { identity: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology: { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] },
    scope: { platform: 'xiaohongshu', kol_uid: 'kol-1', selection_artifact_id: 'sel-1', selection_version: '1' },
    data: {
      identity: {
        nickname: '达人甲',
        avatar_url: null,
        homepage_url: 'https://xhs.com/kol-1',
        bio: '美妆博主',
        verification: true,
        region: '上海',
      },
      metrics: {
        followers: 130000,
        following: 120,
        posts: 30,
        likes: 5000,
        active_followers: 90000,
        active_follower_rate: 0.69,
        growth_rate: 0.08,
        engagement_total: 1000,
        avg_engagement: 210,
      },
      audience: {
        gender_distribution: [{ key: 'female', label: '女性', value: 70, share: 0.7 }],
        age_distribution: [{ key: '18-24', label: '18-24', value: 40, share: 0.4 }],
        region_distribution: [{ key: 'sh', label: '上海', value: 31, share: 0.31 }],
        interest_distribution: [{ key: 'beauty', label: '美妆', value: 62, share: 0.62 }],
      },
      trend: [
        { date: '2026-07-01', followers: 120000, engagement: 900, posts: 2 },
        { date: '2026-07-08', followers: 130000, engagement: 1100, posts: 3 },
      ],
      latest_posts: [
        {
          platform: 'xiaohongshu',
          post_id: 'p1',
          title: '夏日美妆分享',
          url: 'https://xhs.com/p1',
          author: '达人甲',
          published_at: '2026-07-15',
          likes: 5000,
          comments: 300,
          shares: 100,
          engagement: 5400,
        },
        {
          platform: 'xiaohongshu',
          post_id: 'p2',
          title: '秋冬妆容教程',
          url: 'https://xhs.com/p2',
          author: '达人甲',
          published_at: '2026-07-20',
          likes: 4000,
          comments: 200,
          shares: 80,
          engagement: 4280,
        },
      ],
      cache: { hit: true, fetched_at: '2026-08-01T10:00:00', expires_at: '2026-08-02T10:00:00' },
    },
    narrative: { profile_summary: '达人概览', content_strengths: [], commercial_notes: [], risk_notes: [] },
  };
}

describe('KolDetailArtifactDialog', () => {
  it('展示主页、受众分布、趋势与最多 5 条最新帖', () => {
    render(<KolDetailArtifactDialog payload={detail()} onClose={vi.fn()} />);

    expect(screen.getByRole('dialog', { name: '达人甲达人详情' })).toBeVisible();
    expect(screen.getByRole('link', { name: '打开主页' })).toHaveAttribute('href', 'https://xhs.com/kol-1');

    expect(screen.getByText('受众画像')).toBeVisible();
    expect(screen.getByText('女性')).toBeVisible();
    expect(screen.getByText('18-24')).toBeVisible();

    expect(screen.getByText('粉丝趋势')).toBeVisible();

    expect(screen.getByText('最新热帖')).toBeVisible();
    expect(screen.getByText('夏日美妆分享')).toBeVisible();
    expect(screen.getByText('秋冬妆容教程')).toBeVisible();
    expect(screen.getByRole('link', { name: '查看热帖：夏日美妆分享' })).toHaveAttribute('href', 'https://xhs.com/p1');
  });

  it('homepage_url 或帖子 url 缺失时显示「数据受限」且不伪造链接', () => {
    const p = detail();
    p.data.identity.homepage_url = null;
    p.data.latest_posts = [{ ...p.data.latest_posts[0], url: null }];

    render(<KolDetailArtifactDialog payload={p} onClose={vi.fn()} />);

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getAllByText(/数据受限/).length).toBeGreaterThan(0);
    expect(screen.getByText('夏日美妆分享')).toBeVisible();
  });

  it('非 http/https 的主页与帖子链接不渲染为链接（URL 白名单）', () => {
    const p = detail();
    p.data.identity.homepage_url = 'javascript:alert(1)';
    p.data.latest_posts = [{ ...p.data.latest_posts[0], url: 'javascript:alert(1)' }];

    render(<KolDetailArtifactDialog payload={p} onClose={vi.fn()} />);

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('夏日美妆分享')).toBeVisible();
    expect(screen.getAllByText(/数据受限/).length).toBeGreaterThan(0);
  });

  it('点击关闭按钮调用 onClose', () => {
    const onClose = vi.fn();
    render(<KolDetailArtifactDialog payload={detail()} onClose={onClose} />);

    fireEvent.click(screen.getByRole('button', { name: '关闭达人详情' }));

    expect(onClose).toHaveBeenCalled();
  });
});
