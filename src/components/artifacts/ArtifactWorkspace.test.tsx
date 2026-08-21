import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentKolDetailResponse } from '../../api/agent';
import type {
  ApiAgentArtifact,
  ApiAgentArtifactVersion,
  AgentArtifactPayload,
  AnalysisReportPayload,
  BrandReportPayload,
  InsightBoardPayload,
  KolDetailPayload,
  KolSelectionPayload,
} from '../../api/agentArtifacts';
import { exportArtifact, getArtifact, getArtifactVersion, listArtifactReadStates } from '../../api/agentArtifacts';
import { useAgentRun } from '../../hooks/useAgentRun';
import { initialRunRuntime } from '../../state/agentEvents';
import type { RunRuntimeState } from '../../state/agentEvents';
import ArtifactWorkspace, { type ArtifactWorkspaceProps } from './ArtifactWorkspace';

vi.mock('../../api/agentArtifacts', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/agentArtifacts')>();
  return {
    ...actual,
    exportArtifact: vi.fn(),
    getArtifact: vi.fn(),
    getArtifactVersion: vi.fn(),
    listArtifactReadStates: vi.fn(),
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

function analysisReportPayload(): AnalysisReportPayload {
  const rows: Array<Array<string | number | null>> = Array.from(
    { length: 45 },
    (_, index) => [`平台-${index + 1}`, index + 1],
  );
  return {
    schema_version: 'analysis_report_v1',
    module: 'report',
    data_status: 'restricted',
    availability: {
      blocks: { status: 'partial', reason_codes: ['some_rows_missing'] },
      fulfillment: { status: 'partial', reason_codes: ['long_tail_partial'] },
    },
    limitations: [{ code: 'long_tail_partial', message: '部分长尾数据未返回', affected_paths: ['fulfillment'] }],
    methodology,
    title: '跨平台营销报告',
    subject_type: 'mixed',
    scope: { brand: '海底捞', platforms: ['xiaohongshu', 'douyin'] },
    blocks: [
      {
        block_type: 'metric_cards',
        id: 'metrics',
        title: '核心指标',
        cards: [{ key: 'volume', label: '总声量', value: null, unit: '篇', value_type: 'integer' }],
      },
      {
        block_type: 'typed_table',
        id: 'platforms',
        title: '平台明细',
        columns: [
          { key: 'platform', label: '平台', type: 'string' },
          { key: 'volume', label: '声量', type: 'integer' },
        ],
        rows,
      },
    ],
    fulfillment: [{ key: 'requested_items', requested_min: 50, actual_count: 45, status: 'partial', reason: '上游只返回 45 条' }],
    workbook: null,
  };
}

function reportArtifact(overrides?: Partial<ApiAgentArtifact>): ApiAgentArtifact {
  return {
    id: 'report-1',
    module: 'report',
    artifact_type: 'analysis_report_v1',
    parent_artifact_id: null,
    artifact_key: 'report:hash',
    status: 'published',
    latest_version: 2,
    activity_sequence: 10,
    created_at: '2026-08-01T12:00:00',
    updated_at: '2026-08-01T12:00:00',
    ...overrides,
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
  'report-1': version => versionOf('report-1', version, analysisReportPayload()),
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
    vi.mocked(exportArtifact).mockReset();
    vi.mocked(exportArtifact).mockResolvedValue(undefined);
    // 默认服务端无任何已读水位（零水位）：存在产物的模块一律视为未读。
    vi.mocked(listArtifactReadStates).mockReset();
    vi.mocked(listArtifactReadStates).mockResolvedValue([]);
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

  it('固定四个一级 Tab，达人包含两个子 Tab', async () => {
    renderWorkspace([]);
    await waitFor(() => expect(listArtifactReadStates).toHaveBeenCalledWith('s1'));

    expect(screen.getByRole('tab', { name: '品牌分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '活动分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '达人' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '通用报告' })).toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: '达人' }));

    expect(screen.getByRole('tab', { name: 'KOL 分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '圈选达人' })).toBeVisible();
  });

  it('Draft 更新只显示未读圆点，不自动切换 Tab', async () => {
    // 服务端水位已到 brand 当前最大 seq 5：既有产物不打点。
    vi.mocked(listArtifactReadStates).mockResolvedValue([{ module: 'brand', last_seen_sequence: 5 }]);
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
    await waitFor(() => expect(within(brandTab).getByTestId('unread-dot')).toBeVisible());
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
      module: 'kol-selection',
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
      drafts: [{ artifactId: 'art-detail', module: 'kol-detail', version: 1, status: 'published' }],
    };
    vi.mocked(useAgentRun).mockImplementation(runId => (runId ? completedRun : undefined));
    vi.mocked(getArtifact).mockResolvedValue({
      id: 'art-detail',
      module: 'kol-detail',
      artifact_type: 'kol_detail_v2',
      parent_artifact_id: null,
      artifact_key: 'kol-detail:xiaohongshu:kol-1',
      status: 'published',
      latest_version: 1,
      activity_sequence: 9,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    });

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

  it('生成中的 Draft 不替换已发布视图为空状态', async () => {
    const draft = brandArtifact({
      id: 'brand-draft-2',
      status: 'draft',
      latest_version: 0,
      activity_sequence: 6,
      updated_at: '2026-08-01T11:00:00',
    });
    renderWorkspace([brandArtifact(), draft]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    // 已发布视图仍可见（回退到最近一版已发布产物），并提示生成中。
    expect(await screen.findByText('海底捞')).toBeVisible();
    expect(screen.getByText(/生成中/)).toBeVisible();
    expect(screen.queryByText('完成一次品牌分析后在此展示')).not.toBeInTheDocument();
  });

  it('切换会话后未读水位重新初始化，旧会话高水位不抑制新会话圆点', async () => {
    // 服务端水位按会话区分：s1 已读到 9，s2 只读到 5。
    vi.mocked(listArtifactReadStates).mockImplementation(async sessionId => (
      sessionId === 's1'
        ? [{ module: 'brand', last_seen_sequence: 9 }]
        : [{ module: 'brand', last_seen_sequence: 5 }]
    ));
    // 会话 s1：brand 模块服务端水位 9 = 当前最大 seq，无未读圆点。
    const { rerender } = render(
      <ArtifactWorkspace
        sessionId="s1"
        artifacts={[brandArtifact({ activity_sequence: 9 })]}
        markArtifactSeen={vi.fn()}
        createKolDetail={vi.fn()}
      />,
    );
    await waitFor(() => expect(listArtifactReadStates).toHaveBeenCalledWith('s1'));

    // 切换到会话 s2（组件不卸载）：新会话 brand 服务端水位 5 = 当前最大 seq 5。
    const s2Published = brandArtifact({ id: 'brand-s2', activity_sequence: 5 });
    rerender(
      <ArtifactWorkspace
        sessionId="s2"
        artifacts={[s2Published]}
        markArtifactSeen={vi.fn()}
        createKolDetail={vi.fn()}
      />,
    );
    await waitFor(() => expect(listArtifactReadStates).toHaveBeenCalledWith('s2'));

    // s2 随后到达 seq 7 的 Draft：若沿用 s1 的旧水位 9，7 > 9 不成立，圆点被吞掉。
    const s2Draft = brandArtifact({
      id: 'brand-s2-draft',
      status: 'draft',
      latest_version: 1,
      activity_sequence: 7,
      updated_at: '2026-08-01T11:00:00',
    });
    rerender(
      <ArtifactWorkspace
        sessionId="s2"
        artifacts={[s2Published, s2Draft]}
        markArtifactSeen={vi.fn()}
        createKolDetail={vi.fn()}
      />,
    );

    const brandTab = screen.getByRole('tab', { name: '品牌分析' });
    await waitFor(() => expect(within(brandTab).getByTestId('unread-dot')).toBeVisible());
  });

  it('辅助 Run 失败且无已发布产物时展示错误态而非无限加载', async () => {
    const kolArtifact: ApiAgentArtifact = {
      id: 'kol-selection-1',
      module: 'kol-selection',
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
      run_id: 'detail-run-fail',
      artifact_id: null,
      cached: false,
      detail: null,
    } satisfies AgentKolDetailResponse);
    const failedRun: RunRuntimeState = {
      ...initialRunRuntime('detail-run-fail'),
      status: 'failed',
      connection: 'closed',
      drafts: [],
    };
    vi.mocked(useAgentRun).mockImplementation(runId => (runId ? failedRun : undefined));

    renderWorkspace([kolArtifact], createKolDetail);
    fireEvent.click(screen.getByRole('tab', { name: '达人' }));
    fireEvent.click(screen.getByRole('tab', { name: '圈选达人' }));
    await screen.findByText('达人甲');

    fireEvent.click(screen.getByRole('button', { name: /查看达人甲详情/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/达人详情生成失败/);
    expect(screen.queryByText('正在生成达人详情…')).not.toBeInTheDocument();
  });

  // ----------------------------------------------------------------------- //
  // 未读水位：服务端初始化 + 离线未读 + 点击上报（C2）
  // ----------------------------------------------------------------------- //

  it('按服务端已读水位初始化：离线期间的新产物打点，点击 Tab 后上报并清除', async () => {
    // 服务端水位 brand=5：当前最大 seq 5 的既有产物不打点。
    vi.mocked(listArtifactReadStates).mockResolvedValue([{ module: 'brand', last_seen_sequence: 5 }]);
    const markArtifactSeen = vi.fn();
    const { rerender } = render(
      <ArtifactWorkspace
        sessionId="s1"
        artifacts={[brandArtifact()]}
        markArtifactSeen={markArtifactSeen}
        createKolDetail={vi.fn()}
      />,
    );

    // 等品牌内容渲染完成（此时水位已就绪），确认无未读圆点。
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));
    await screen.findByText('海底捞');
    expect(markArtifactSeen).toHaveBeenCalledWith('brand', 5);
    expect(screen.queryAllByTestId('unread-dot')).toHaveLength(0);

    // 离线期间发布的新产物（seq 7 > 服务端水位 5）→ 刷新后圆点出现。
    const offlinePublished = brandArtifact({
      id: 'brand-offline',
      activity_sequence: 7,
      updated_at: '2026-08-01T11:00:00',
    });
    rerender(
      <ArtifactWorkspace
        sessionId="s1"
        artifacts={[brandArtifact(), offlinePublished]}
        markArtifactSeen={markArtifactSeen}
        createKolDetail={vi.fn()}
      />,
    );
    const brandTab = screen.getByRole('tab', { name: '品牌分析' });
    await waitFor(() => expect(within(brandTab).getByTestId('unread-dot')).toBeVisible());

    // 点击 Tab 后按 max(旧, 新) 上报并清除圆点。
    fireEvent.click(brandTab);
    expect(markArtifactSeen).toHaveBeenCalledWith('brand', 7);
    await waitFor(() => expect(within(brandTab).queryByTestId('unread-dot')).toBeNull());
  });

  it('达人 Tab 圆点聚合 kol-selection / kol-analysis 任一模块的未读', async () => {
    const kolAnalysis: ApiAgentArtifact = {
      id: 'kol-analysis-1',
      module: 'kol-analysis',
      artifact_type: 'kol_analysis_v2',
      parent_artifact_id: null,
      artifact_key: 'kol-analysis:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 8,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    // 零水位（服务端无记录）：kol-analysis 有产物即未读。
    renderWorkspace([kolAnalysis]);

    const kolTab = screen.getByRole('tab', { name: '达人' });
    await waitFor(() => expect(within(kolTab).getByTestId('unread-dot')).toBeVisible());
  });

  it('kol-detail 产物更新不点亮达人 Tab 主圆点', async () => {
    const kolDetail: ApiAgentArtifact = {
      id: 'kol-detail-1',
      module: 'kol-detail',
      artifact_type: 'kol_detail_v2',
      parent_artifact_id: null,
      artifact_key: 'kol-detail:xiaohongshu:kol-1',
      status: 'published',
      latest_version: 1,
      activity_sequence: 9,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    // 同场放一个 brand 未读产物作为「水位已就绪」信号：brand 打点而达人不打点。
    renderWorkspace([kolDetail, brandArtifact()]);

    const brandTab = screen.getByRole('tab', { name: '品牌分析' });
    await waitFor(() => expect(within(brandTab).getByTestId('unread-dot')).toBeVisible());
    const kolTab = screen.getByRole('tab', { name: '达人' });
    expect(within(kolTab).queryByTestId('unread-dot')).toBeNull();
  });

  it('点击达人 Tab 按 kol-selection / kol-analysis 实际模块分别上报水位', async () => {
    const markArtifactSeen = vi.fn();
    const kolSelection: ApiAgentArtifact = {
      id: 'kol-selection-1',
      module: 'kol-selection',
      artifact_type: 'kol_selection_v3',
      parent_artifact_id: null,
      artifact_key: 'kol-selection:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 8,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    const kolAnalysis: ApiAgentArtifact = {
      id: 'kol-analysis-1',
      module: 'kol-analysis',
      artifact_type: 'kol_analysis_v2',
      parent_artifact_id: null,
      artifact_key: 'kol-analysis:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 6,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    renderWorkspace([kolSelection, kolAnalysis], vi.fn(), markArtifactSeen);

    const kolTab = screen.getByRole('tab', { name: '达人' });
    await waitFor(() => expect(within(kolTab).getByTestId('unread-dot')).toBeVisible());
    fireEvent.click(kolTab);

    expect(markArtifactSeen).toHaveBeenCalledWith('kol-selection', 8);
    expect(markArtifactSeen).toHaveBeenCalledWith('kol-analysis', 6);
    await waitFor(() => expect(within(kolTab).queryByTestId('unread-dot')).toBeNull());
  });

  // ----------------------------------------------------------------------- //
  // Excel 导出按钮（C2）
  // ----------------------------------------------------------------------- //

  it('已发布品牌产物显示导出按钮，并按当前下拉查看的版本导出', async () => {
    renderWorkspace([brandArtifact()]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    const exportButton = await screen.findByRole('button', { name: '导出 Excel' });
    expect(exportButton).toBeEnabled();

    // 切到历史版本 v1 后导出：导出版本与界面查看版本一致。
    fireEvent.change(screen.getByRole('combobox', { name: '版本选择' }), { target: { value: '1' } });
    await waitFor(() => expect(getArtifactVersion).toHaveBeenCalledWith('brand-1', 1));

    fireEvent.click(exportButton);
    await waitFor(() => expect(exportArtifact).toHaveBeenCalledWith('brand-1', 1));
  });

  it('已发布圈选达人产物显示导出按钮', async () => {
    const kolSelection: ApiAgentArtifact = {
      id: 'kol-selection-1',
      module: 'kol-selection',
      artifact_type: 'kol_selection_v3',
      parent_artifact_id: null,
      artifact_key: 'kol-selection:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 8,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    renderWorkspace([kolSelection]);
    fireEvent.click(screen.getByRole('tab', { name: '圈选达人' }));
    await screen.findByText('达人甲');

    const exportButton = screen.getByRole('button', { name: '导出 Excel' });
    fireEvent.click(exportButton);
    await waitFor(() => expect(exportArtifact).toHaveBeenCalledWith('kol-selection-1', 1));
  });

  it('已发布活动产物也按当前精确版本提供 Excel 导出', async () => {
    const campaign: ApiAgentArtifact = {
      id: 'campaign-1',
      module: 'campaign',
      artifact_type: 'campaign_report_v2',
      parent_artifact_id: null,
      artifact_key: 'campaign:hash',
      status: 'published',
      latest_version: 1,
      activity_sequence: 4,
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    renderWorkspace([campaign]);
    fireEvent.click(screen.getByRole('tab', { name: '活动分析' }));

    // 即便 payload 尚在加载，导出身份仍绑定当前 Artifact + Version。
    await waitFor(() => expect(getArtifactVersion).toHaveBeenCalledWith('campaign-1', 1));
    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));
    await waitFor(() => expect(exportArtifact).toHaveBeenCalledWith('campaign-1', 1));
  });

  it('Draft（非 published）产物不显示导出按钮', async () => {
    const draft = brandArtifact({ status: 'draft', latest_version: 1 });
    renderWorkspace([draft]);
    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    await waitFor(() => expect(getArtifactVersion).toHaveBeenCalledWith('brand-1', 1));
    expect(screen.queryByRole('button', { name: '导出 Excel' })).toBeNull();
  });

  it('通用报告 Tab 展示长尾全部行，并按当前版本进入同版 Excel 导出', async () => {
    renderWorkspace([reportArtifact()]);
    fireEvent.click(screen.getByRole('tab', { name: '通用报告' }));

    expect(await screen.findByRole('heading', { name: '跨平台营销报告' })).toBeVisible();
    expect(screen.getByText('平台-45')).toBeVisible();
    expect(screen.getAllByText('数据受限').length).toBeGreaterThan(0);

    fireEvent.change(screen.getByRole('combobox', { name: '版本选择' }), { target: { value: '1' } });
    await waitFor(() => expect(getArtifactVersion).toHaveBeenCalledWith('report-1', 1));
    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));
    await waitFor(() => expect(exportArtifact).toHaveBeenCalledWith('report-1', 1));
  });
});
