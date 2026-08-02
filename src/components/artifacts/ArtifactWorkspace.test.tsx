import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentKolDetailResponse } from '../../api/agent';
import type {
  ApiAgentArtifact,
  ApiAgentArtifactVersion,
  AgentArtifactPayload,
  BrandReportPayload,
  InsightBoardPayload,
  KolDetailPayload,
  KolSelectionPayload,
} from '../../api/agentArtifacts';
import { getArtifactVersion } from '../../api/agentArtifacts';
import { useAgentRun } from '../../hooks/useAgentRun';
import { initialRunRuntime } from '../../state/agentEvents';
import type { RunRuntimeState } from '../../state/agentEvents';
import ArtifactWorkspace, { type ArtifactWorkspaceProps } from './ArtifactWorkspace';

vi.mock('../../api/agentArtifacts', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/agentArtifacts')>();
  return {
    ...actual,
    getArtifact: vi.fn(),
    getArtifactVersion: vi.fn(),
    markArtifactRead: vi.fn(),
  };
});

vi.mock('../../hooks/useAgentRun', async importOriginal => {
  const actual = await importOriginal<typeof import('../../hooks/useAgentRun')>();
  return { ...actual, useAgentRun: vi.fn() };
});

const methodology = { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] };

function brandArtifact(overrides?: Partial<ApiAgentArtifact>): ApiAgentArtifact {
  return {
    id: 'brand-1',
    module: 'brand',
    artifact_type: 'brand_report_v3',
    parent_artifact_id: null,
    artifact_key: 'brand:海底捞',
    status: 'published',
    latest_version: 2,
    activity_sequence: 5,
    created_at: '2026-08-01T10:00:00',
    updated_at: '2026-08-01T10:00:00',
    ...overrides,
  };
}

function brandPayload(): BrandReportPayload {
  return {
    schema_version: 'brand_report_v3',
    module: 'brand',
    data_status: 'complete',
    availability: { overview: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology,
    scope: {
      brand: '海底捞',
      period: { start: '2026-07-01', end: '2026-07-31', timezone: 'Asia/Shanghai' },
      platforms: ['xiaohongshu'],
      keywords: [],
      comparison_mode: 'none',
    },
    data: {
      overview: {
        total_volume: 120000,
        total_engagement: 3400,
        total_posts: 86,
        sentiment_score: 0.72,
        platforms: [],
      },
      comparisons: {
        mom: { status: 'not_requested', baseline_period: null, metrics: [] },
        yoy: { status: 'not_requested', baseline_period: null, metrics: [] },
      },
      sentiment: {
        summary: {
          positive: { count: 60, share: 0.6 },
          neutral: { count: 20, share: 0.2 },
          negative: { count: 20, share: 0.2 },
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
    narrative: { executive_summary: '品牌总结', findings: [], recommendations: [] },
  };
}

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

function kolSelectionPayload(): KolSelectionPayload {
  return {
    schema_version: 'kol_selection_v3',
    module: 'kol',
    data_status: 'complete',
    availability: { items: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology,
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
      items: [{
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
        reasons: [],
        missing_fields: [],
        score_snapshot: {
          version: 'kol_score_v2',
          total: 85,
          rating: '重点推荐',
          stars: '★★★★★',
          data_completeness: 92,
          dimensions: SCORE_DIMENSIONS,
        },
      }],
      summary: {
        candidate_count: 30,
        selected_count: 1,
        platform_distribution: [],
        rating_distribution: [],
      },
    },
    narrative: { selection_summary: '圈选说明', fit_findings: [], risk_notes: [], usage_advice: [] },
  };
}

function kolDetailPayload(): KolDetailPayload {
  return {
    schema_version: 'kol_detail_v2',
    module: 'kol',
    data_status: 'complete',
    availability: { identity: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology,
    scope: { platform: 'xiaohongshu', kol_uid: 'kol-1', selection_artifact_id: 'kol-selection-1', selection_version: '1' },
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
      latest_posts: [{
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
      }],
      cache: { hit: true, fetched_at: '2026-08-01T10:00:00', expires_at: '2026-08-02T10:00:00' },
    },
    narrative: { profile_summary: '达人概览', content_strengths: [], commercial_notes: [], risk_notes: [] },
  };
}

function insightPayload(): InsightBoardPayload {
  return {
    schema_version: 'insight_board_v1',
    title: '竞品钻取',
    data_status: 'complete',
    availability: { blocks: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology,
    scope: { summary: '', period: null, platforms: [], brand: '竞品', campaign: null, kol_uid: null },
    parent_artifact_id: 'brand-1',
    narrative: { summary: '钻取说明', findings: [] },
    data: [{ block_type: 'metric_grid', title: '核心指标', cards: [{ key: 'volume', label: '声量', value: 120 }] }],
  };
}

function versionOf(artifactId: string, version: number, payload: AgentArtifactPayload): ApiAgentArtifactVersion {
  return {
    id: `${artifactId}-v${version}`,
    artifact_id: artifactId,
    version,
    schema_version: payload.schema_version,
    data_status: payload.data_status,
    payload: payload as unknown as Record<string, unknown>,
    evidence_refs: [],
    created_at: '2026-08-01T10:00:00',
  };
}

const VERSIONS: Record<string, (version: number) => ApiAgentArtifactVersion> = {
  'brand-1': version => versionOf('brand-1', version, brandPayload()),
  'kol-selection-1': () => versionOf('kol-selection-1', 1, kolSelectionPayload()),
  'art-detail': () => versionOf('art-detail', 1, kolDetailPayload()),
  'insight-child-1': () => versionOf('insight-child-1', 1, insightPayload()),
};

describe('ArtifactWorkspace', () => {
  beforeEach(() => {
    vi.mocked(getArtifactVersion).mockReset();
    vi.mocked(getArtifactVersion).mockImplementation(async (artifactId, version) => {
      const factory = VERSIONS[artifactId];
      if (!factory) throw new Error('NOT_FOUND');
      return factory(version);
    });
    vi.mocked(useAgentRun).mockReset();
  });

  function renderWorkspace(artifacts: ApiAgentArtifact[], createKolDetail = vi.fn(), markArtifactSeen = vi.fn()) {
    return render(
      <ArtifactWorkspace
        sessionId="s1"
        artifacts={artifacts}
        markArtifactSeen={markArtifactSeen}
        createKolDetail={createKolDetail as ArtifactWorkspaceProps['createKolDetail']}
      />,
    );
  }

  it('固定三个一级 Tab，达人包含两个子 Tab', () => {
    renderWorkspace([]);

    expect(screen.getByRole('tab', { name: '品牌分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '活动分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '达人' })).toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: '达人' }));

    expect(screen.getByRole('tab', { name: 'KOL 分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '圈选达人' })).toBeVisible();
  });

  it('Draft 更新只显示未读圆点，不自动切换 Tab', () => {
    const { rerender } = renderWorkspace([brandArtifact()]);
    expect(screen.getByRole('tab', { name: '达人' })).toHaveAttribute('aria-selected', 'true');

    const draft = brandArtifact({
      id: 'brand-draft',
      status: 'draft',
      latest_version: 1,
      activity_sequence: 6,
      updated_at: '2026-08-01T11:00:00',
    });
    rerender(
      <ArtifactWorkspace
        sessionId="s1"
        artifacts={[brandArtifact(), draft]}
        markArtifactSeen={vi.fn()}
        createKolDetail={vi.fn()}
      />,
    );

    // 不自动切换：仍停留在达人 Tab。
    expect(screen.getByRole('tab', { name: '达人' })).toHaveAttribute('aria-selected', 'true');
    // 品牌 Tab 出现未读圆点。
    const brandTab = screen.getByRole('tab', { name: '品牌分析' });
    expect(within(brandTab).getByTestId('unread-dot')).toBeVisible();
  });

  it('已发布产物显示历史版本选择器，可切换历史版本', async () => {
    renderWorkspace([brandArtifact()]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    const versionSelect = await screen.findByRole('combobox', { name: '版本选择' });
    expect(versionSelect).toBeVisible();
    expect(within(versionSelect).getAllByRole('option').map(option => option.textContent)).toEqual(['v1', 'v2']);

    fireEvent.change(versionSelect, { target: { value: '1' } });
    await waitFor(() => expect(getArtifactVersion).toHaveBeenCalledWith('brand-1', 1));
    // 切换历史版本后仍渲染视图。
    expect(await screen.findByText('海底捞')).toBeVisible();
  });

  it('restricted 产物展示受限标记', async () => {
    const payload = brandPayload();
    payload.data_status = 'restricted';
    payload.limitations = [{ code: 'no_data', message: '部分平台未采集到数据', affected_paths: ['data.regions'] }];
    vi.mocked(getArtifactVersion).mockResolvedValue(versionOf('brand-1', 1, payload));

    renderWorkspace([brandArtifact({ latest_version: 1 })]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    expect((await screen.findAllByText('数据受限')).length).toBeGreaterThan(0);
    expect(screen.getByText('部分平台未采集到数据')).toBeVisible();
  });

  it('Insight Board 作为父产物的子分析列表展示', async () => {
    const child: ApiAgentArtifact = {
      id: 'insight-child-1',
      module: 'brand',
      artifact_type: 'insight_board_v1',
      parent_artifact_id: 'brand-1',
      artifact_key: 'insight:brand-1:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 7,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    renderWorkspace([brandArtifact(), child]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    expect(await screen.findByText('钻取分析')).toBeVisible();
    expect(await screen.findByText('竞品钻取')).toBeVisible();
  });

  it('点击圈选名单中的 KOL 通过 createKolDetail 订阅辅助 Run 并打开详情弹层', async () => {
    const kolArtifact: ApiAgentArtifact = {
      id: 'kol-selection-1',
      module: 'kol',
      artifact_type: 'kol_selection_v3',
      parent_artifact_id: null,
      artifact_key: 'kol-selection:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 8,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    const createKolDetail = vi.fn().mockResolvedValue({
      run_id: 'detail-run-1',
      artifact_id: null,
      cached: false,
      detail: null,
    } satisfies AgentKolDetailResponse);
    const completedRun: RunRuntimeState = {
      ...initialRunRuntime('detail-run-1'),
      status: 'completed',
      connection: 'closed',
      artifactsVersion: 1,
      drafts: [{ artifactId: 'art-detail', module: 'kol', version: 1, status: 'published' }],
    };
    vi.mocked(useAgentRun).mockImplementation(runId => (runId ? completedRun : undefined));

    renderWorkspace([kolArtifact], createKolDetail);
    fireEvent.click(screen.getByRole('tab', { name: '达人' }));
    fireEvent.click(screen.getByRole('tab', { name: '圈选达人' }));
    await screen.findByText('达人甲');

    fireEvent.click(screen.getByRole('button', { name: /查看达人甲详情/ }));

    await waitFor(() => expect(createKolDetail).toHaveBeenCalledWith(
      's1',
      'xiaohongshu',
      'kol-1',
      expect.objectContaining({ artifact_id: 'kol-selection-1', version: '1' }),
    ));
    expect(await screen.findByRole('dialog', { name: '达人甲达人详情' })).toBeVisible();
  });
});
