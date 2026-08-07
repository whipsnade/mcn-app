import { expect, test, type Page } from '@playwright/test';

// --------------------------------------------------------------------------- //
// 统一 Artifact 工作区 E2E（design §13.2 / Task 25）。
//
// 固定三个 BI Tab（品牌分析/活动分析/达人）与达人的两个子 Tab
// （KOL 分析/圈选达人）；已发布品牌产物渲染章节 + 版本选择 + restricted
// 徽标；模块出现更高产物时打未读圆点、查看后清除；子分析挂父产物下；
// 点击圈选达人列表打开达人详情弹窗；Excel 导出走新 artifact export 路由。
//
// 全部产物数据经 page.route 注入 NEW Agent Artifact fixture（真实
// artifact_id / parent_artifact_id / activity_sequence），不触碰旧
// /analysis-reports、/sessions/{id}/reports 或旧 brand export 路由。
// --------------------------------------------------------------------------- //

const BASE_TS = '2026-08-02T10:00:00';
const EXPORT_FILENAME = '海底捞_品牌社媒分析报告_v2.xlsx';

interface SseEvent {
  seq: number;
  event: string;
  payload?: Record<string, unknown>;
}

// --------------------------------------------------------------------------- //
// 产物 fixture
// --------------------------------------------------------------------------- //

function artifactMeta(
  id: string,
  module: string,
  artifactType: string,
  parentArtifactId: string | null,
  latestVersion: number,
  activitySequence: number,
): Record<string, unknown> {
  return {
    id,
    module,
    artifact_type: artifactType,
    parent_artifact_id: parentArtifactId,
    artifact_key: artifactType.split('_')[0],
    status: 'published',
    latest_version: latestVersion,
    activity_sequence: activitySequence,
    created_at: BASE_TS,
    updated_at: BASE_TS,
  };
}

function methodologyFixture(): Record<string, unknown> {
  return { data_as_of: '2026-07-01T00:00:00Z', source_names: ['品牌声量查询'], notes: [] };
}

// brand_report_v3 完整快照（章节全 complete）。
function brandPayloadComplete(): Record<string, unknown> {
  return {
    schema_version: 'brand_report_v3',
    module: 'brand',
    data_status: 'complete',
    scope: {
      brand: '海底捞',
      period: { start: '2026-06-01', end: '2026-06-30', timezone: 'Asia/Shanghai' },
      platforms: ['xiaohongshu', 'douyin'],
      keywords: ['海底捞'],
      comparison_mode: 'mom_yoy',
    },
    data: {
      overview: {
        total_volume: 2000,
        total_engagement: 147000,
        total_posts: 320,
        sentiment_score: 0.78,
        platforms: [
          { platform: 'xiaohongshu', volume: 1200, engagement: 56000, posts: 200, share_of_voice: 60, sentiment_score: 0.8 },
          { platform: 'douyin', volume: 800, engagement: 91000, posts: 120, share_of_voice: 40, sentiment_score: 0.75 },
        ],
      },
      comparisons: {
        mom: {
          status: 'ok',
          baseline_period: { start: '2026-05-01', end: '2026-05-31', timezone: 'Asia/Shanghai' },
          metrics: [{ metric: '总声量', current: 2000, baseline: 1800, delta: 200, rate: 0.111 }],
        },
        yoy: { status: 'not_requested', baseline_period: null, metrics: [] },
      },
      sentiment: {
        summary: { positive: { count: 1500, share: 0.75 }, neutral: { count: 400, share: 0.2 }, negative: { count: 100, share: 0.05 } },
        by_platform: [
          { platform: 'xiaohongshu', positive: { count: 900, share: 0.75 }, neutral: { count: 240, share: 0.2 }, negative: { count: 60, share: 0.05 } },
        ],
      },
      daily_trend: [
        { date: '2026-06-01', platform: 'xiaohongshu', volume: 60, engagement: 3000, positive: 45, neutral: 10, negative: 5 },
        { date: '2026-06-15', platform: 'xiaohongshu', volume: 320, engagement: 21000, positive: 240, neutral: 60, negative: 20 },
        { date: '2026-06-30', platform: 'xiaohongshu', volume: 90, engagement: 5000, positive: 70, neutral: 15, negative: 5 },
      ],
      content_types: [{ platform: 'xiaohongshu', type: '图文', posts: 150, volume: 1100, engagement: 30000 }],
      creator_tiers: [{ platform: 'xiaohongshu', tier: '头部', creator_count: 50, posts: 100, volume: 600, engagement: 20000 }],
      organic_vs_paid: [{ platform: 'xiaohongshu', kind: 'organic', posts: 120, volume: 1500, engagement: 40000 }],
      regions: [{ region: '广东', volume: 400, share: 0.2, sentiment_score: 0.8 }],
      topics: [{ topic: '海底捞服务', volume: 800, engagement: 20000, sentiment_score: 0.9 }],
      top_posts: [
        { platform: 'xiaohongshu', post_id: 'xhs-1', title: '海底捞隐藏吃法合集', url: 'https://www.xiaohongshu.com/explore/xhs-1', author: '吃货小分队', published_at: '2026-06-20', likes: 8000, comments: 900, shares: 600, engagement: 9500 },
      ],
    },
    narrative: {
      executive_summary: '品牌整体声量健康，负面集中于排队体验。',
      findings: [{ title: '正面声量占主导', detail: '正面占比 75%。', supporting_paths: ['data.sentiment'] }],
      recommendations: [{ title: '优化排队体验', action: '加强门店排队管理。', rationale: '负面集中于排队。', supporting_paths: ['data.sentiment'] }],
    },
    availability: {},
    limitations: [],
    methodology: methodologyFixture(),
  };
}

// brand_report_v3 restricted 快照：数据受限徽标 + 部分指标受限。
function brandPayloadRestricted(): Record<string, unknown> {
  const complete = brandPayloadComplete() as {
    data_status: string;
    data: { overview: Record<string, unknown> };
  };
  return {
    ...complete,
    data_status: 'restricted',
    data: {
      ...complete.data,
      overview: { ...complete.data.overview, total_volume: null, total_engagement: null },
    },
    limitations: [{ code: 'insufficient_balance', message: '积分不足，部分数据受限', affected_paths: ['data.overview'] }],
  };
}

function kolSelectionPayload(): Record<string, unknown> {
  return {
    schema_version: 'kol_selection_v3',
    module: 'kol',
    data_status: 'complete',
    scope: {
      brand: null,
      category: '美食',
      campaign: null,
      platforms: ['xiaohongshu'],
      audience: { regions: [], age_ranges: [], interests: [] },
      filters: { budget_min: null, budget_max: null, follower_min: null, follower_max: null },
    },
    data: {
      scoring: {
        version: 'kol_score_v2',
        method: 'weighted_sum',
        weights: {
          engagement: 0.2, active_follower: 0.2, content: 0.15, followers: 0.15,
          industry_interest: 0.1, target_region: 0.05, target_age: 0.05, engagement_follower_ratio: 0.1,
        },
        missing_value_policy: 'missing_as_zero',
      },
      items: [
        {
          rank: 1,
          platform: 'xiaohongshu',
          kol_uid: 'uid-1',
          nickname: '美食小A',
          avatar_url: null,
          homepage_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
          followers: 120000,
          active_followers: 80000,
          active_follower_rate: 0.67,
          growth_rate: 0.05,
          engagement_total: 56000,
          avg_engagement: 1200,
          likes: 30000,
          comments: 1200,
          shares: 800,
          quoted_price: 12000,
          reasons: ['互动率高'],
          missing_fields: [],
          score_snapshot: {
            version: 'kol_score_v2',
            total: 88,
            rating: 'S',
            stars: '★★★★★',
            data_completeness: 100,
            dimensions: {
              engagement: { raw_score: 90, weight: 0.2, weighted_score: 18, source: 'engagement', missing_reason: null },
              active_follower: { raw_score: 85, weight: 0.2, weighted_score: 17, source: null, missing_reason: null },
              content: { raw_score: 80, weight: 0.15, weighted_score: 12, source: null, missing_reason: null },
              followers: { raw_score: 70, weight: 0.15, weighted_score: 10.5, source: null, missing_reason: null },
              industry_interest: { raw_score: 90, weight: 0.1, weighted_score: 9, source: null, missing_reason: null },
              target_region: { raw_score: 0, weight: 0.05, weighted_score: 0, source: null, missing_reason: '无法匹配目标地区' },
              target_age: { raw_score: 80, weight: 0.05, weighted_score: 4, source: null, missing_reason: null },
              engagement_follower_ratio: { raw_score: 88, weight: 0.1, weighted_score: 8.8, source: null, missing_reason: null },
            },
          },
        },
      ],
      summary: {
        candidate_count: 20,
        selected_count: 1,
        platform_distribution: [{ key: 'xiaohongshu', label: '小红书', count: 1, share: 1 }],
        rating_distribution: [{ key: 'S', label: 'S', count: 1, share: 1 }],
      },
    },
    narrative: {
      selection_summary: '按互动率与预算匹配度圈选。',
      fit_findings: [],
      risk_notes: [],
      usage_advice: [],
    },
    availability: {},
    limitations: [],
    methodology: methodologyFixture(),
  };
}

function kolDetailPayload(): Record<string, unknown> {
  return {
    schema_version: 'kol_detail_v2',
    module: 'kol',
    data_status: 'complete',
    scope: { platform: 'xiaohongshu', kol_uid: 'uid-1', selection_artifact_id: null, selection_version: null },
    data: {
      identity: {
        nickname: '美食小A',
        avatar_url: null,
        homepage_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
        bio: '美食探店博主',
        verification: true,
        region: '上海',
      },
      metrics: {
        followers: 120000, following: 300, posts: 500, likes: 30000,
        active_followers: 80000, active_follower_rate: 0.67, growth_rate: 0.05,
        engagement_total: 56000, avg_engagement: 1200,
      },
      audience: {
        gender_distribution: [{ key: 'female', label: '女性', value: 90000, share: 0.75 }],
        age_distribution: [{ key: '18-24', label: '18-24岁', value: 50000, share: 0.42 }],
        region_distribution: [{ key: 'sh', label: '上海', value: 30000, share: 0.25 }],
        interest_distribution: [{ key: 'food', label: '美食', value: 70000, share: 0.58 }],
      },
      trend: [
        { date: '2026-06-01', followers: 110000, engagement: 40000, posts: 10 },
        { date: '2026-07-01', followers: 120000, engagement: 56000, posts: 12 },
      ],
      latest_posts: [
        { platform: 'xiaohongshu', post_id: 'xhs-p1', title: '上海宝藏美食合集', url: 'https://www.xiaohongshu.com/explore/xhs-p1', author: '美食小A', published_at: '2026-07-20', likes: 8000, comments: 900, shares: 600, engagement: 9500 },
      ],
      cache: { hit: false, fetched_at: '2026-08-02T10:00:00Z', expires_at: '2026-08-03T10:00:00Z' },
    },
    narrative: {
      profile_summary: '美食领域头部达人，互动表现优异。',
      content_strengths: [{ title: '内容质量高', detail: '探店视频平均互动破千。', supporting_paths: [] }],
      commercial_notes: [],
      risk_notes: [{ title: '内容单一', detail: '以美食探店为主。', supporting_paths: [] }],
    },
    availability: {},
    limitations: [],
    methodology: methodologyFixture(),
  };
}

function insightPayload(): Record<string, unknown> {
  return {
    schema_version: 'insight_board_v1',
    title: '情感钻取分析',
    scope: { summary: '针对品牌情感维度的钻取。', period: null, platforms: ['xiaohongshu'], brand: '海底捞', campaign: null, kol_uid: null },
    parent_artifact_id: 'brand-art',
    narrative: { summary: '正面情绪主导。', findings: [] },
    data: [
      { block_type: 'metric_grid', title: '情感概览', cards: [{ label: '正面占比', value: 75 }, { label: '负面占比', value: 5 }] },
      { block_type: 'markdown', title: '结论', text: '整体情感健康。' },
    ],
    data_status: 'complete',
    availability: {},
    limitations: [],
    methodology: methodologyFixture(),
  };
}

// --------------------------------------------------------------------------- //
// 共享辅助
// --------------------------------------------------------------------------- //

async function login(page: Page, phone: string) {
  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(phone);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();
}

/** 小视口（<1280px）先切到 BI 面板（xl 桌面端常显，无需切换）。 */
async function ensureBiPane(page: Page) {
  if ((page.viewportSize()?.width ?? 1440) >= 1280) return;
  await page.getByRole('navigation', { name: '移动工作区导航' }).getByRole('button', { name: '分析报告' }).click();
}

function sseBody(runId: string, events: SseEvent[]): string {
  return events.map(({ seq, event, payload = {} }) => (
    `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify({ ...payload, run_id: runId })}\n\n`
  )).join('');
}

interface ArtifactWorkspaceRoutes {
  sessionId: string;
  sessionTitle: string;
  /** 每次目录刷新返回的 Artifact 列表。 */
  artifacts: () => Array<Record<string, unknown>>;
  /** (artifactId, version) → 版本 payload。 */
  versionPayload: (artifactId: string, version: number) => Record<string, unknown>;
  /** artifactId → 详情 meta。 */
  artifactMeta: (artifactId: string) => Record<string, unknown>;
  /** 可选：会话附带 Run 的 SSE 事件体（未提供则 Run 不存在）。 */
  runEvents?: () => string;
  runId?: string;
  /** 可选：服务端已读水位（未提供则空表 = 各模块零水位）。 */
  readStates?: () => Array<{ module: string; last_seen_sequence: number }>;
}

async function installArtifactRoutes(page: Page, opts: ArtifactWorkspaceRoutes) {
  const { sessionId, sessionTitle } = opts;
  const session = {
    id: sessionId,
    title: sessionTitle,
    status: 'active',
    created_at: '2026-08-01T10:00:00',
    updated_at: BASE_TS,
  };
  const detail = () => ({
    ...session,
    messages: [],
    runs: opts.runId ? [{ id: opts.runId, session_id: sessionId, status: 'running' }] : [],
  });

  await page.route('**/api/v1/wallet', route => route.fulfill({ json: { balance: 100, reserved: 0, available: 100 } }));
  await page.route('**/api/v1/favorites', route => route.fulfill({ json: [] }));
  await page.route('**/api/v1/agent/sessions', route => route.fulfill({ json: [session] }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}/artifacts`, route => route.fulfill({ json: opts.artifacts() }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}/artifact-read-state`, route => route.fulfill({
    json: { module: 'brand', last_seen_sequence: 0 },
  }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}/artifact-read-states`, route => route.fulfill({
    json: opts.readStates ? opts.readStates() : [],
  }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}/kol-details`, route => route.fulfill({
    status: 201,
    json: { run_id: null, artifact_id: 'kol-detail-art', cached: false, detail: null },
  }));

  // 产物详情 / 版本 / 导出统一入口。
  await page.route('**/api/v1/agent/artifacts/**', route => {
    const path = new URL(route.request().url()).pathname;
    const versions = /\/api\/v1\/agent\/artifacts\/([^/]+)\/versions\/(\d+)$/.exec(path);
    if (versions) return route.fulfill({ json: {
      id: `${versions[1]}-v${versions[2]}`,
      artifact_id: versions[1],
      version: Number(versions[2]),
      schema_version: 'brand_report_v3',
      data_status: 'complete',
      payload: opts.versionPayload(versions[1], Number(versions[2])),
      evidence_refs: null,
      created_at: BASE_TS,
    } });
    const exportMatch = /\/api\/v1\/agent\/artifacts\/([^/]+)\/export$/.exec(path);
    if (exportMatch) return route.fulfill({
      status: 200,
      body: Buffer.from('xlsx'),
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(EXPORT_FILENAME)}`,
      },
    });
    const meta = /\/api\/v1\/agent\/artifacts\/([^/]+)$/.exec(path);
    if (meta) return route.fulfill({ json: opts.artifactMeta(meta[1]) });
    return route.fulfill({ status: 404, json: { detail: 'artifact_not_found' } });
  });

  if (opts.runId && opts.runEvents) {
    await page.route(`**/api/v1/agent/runs/${opts.runId}/events`, route => route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: opts.runEvents!(),
    }));
  }
}

// --------------------------------------------------------------------------- //
// 1. 三个 BI Tab + 达人两个子 Tab
// --------------------------------------------------------------------------- //

test('renders the three fixed BI tabs and the two KOL sub-tabs', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  await installArtifactRoutes(page, {
    sessionId: 's-bi',
    sessionTitle: 'BI 会话',
    artifacts: () => [],
    versionPayload: () => ({}),
    artifactMeta: () => ({}),
  });

  await login(page, phone);
  await ensureBiPane(page);

  await expect(page.getByRole('tab', { name: '品牌分析' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '活动分析' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '达人', exact: true })).toBeVisible();

  // 达人默认激活，展开两个子 Tab。
  await expect(page.getByRole('tab', { name: 'KOL 分析' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '圈选达人' })).toBeVisible();

  // 四个旧 Quick 入口不出现。
  for (const name of ['达人推荐', '活动评估', '小红书爆贴', '抖音爆贴']) {
    await expect(page.getByText(name, { exact: true })).toHaveCount(0);
  }
});

// --------------------------------------------------------------------------- //
// 2. 已发布品牌产物：章节 / 版本选择 / restricted 徽标
// --------------------------------------------------------------------------- //

test('renders a published brand artifact with sections, version selector and restricted badge', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const brandArt = artifactMeta('brand-art', 'brand', 'brand_report_v3', null, 2, 10);

  await installArtifactRoutes(page, {
    sessionId: 's-brand',
    sessionTitle: '品牌会话',
    artifacts: () => [brandArt],
    versionPayload: (artifactId, version) => version === 2 ? brandPayloadRestricted() : brandPayloadComplete(),
    artifactMeta: id => artifactMeta(id, 'brand', 'brand_report_v3', null, 2, 10),
  });

  await login(page, phone);
  await ensureBiPane(page);

  await page.getByRole('tab', { name: '品牌分析' }).click();

  // 已发布 + restricted：状态徽标与章节导航渲染，指标区显示「数据受限」。
  await expect(page.getByText('已发布', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('数据受限', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('概览', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('情感分析', { exact: true })).toBeVisible();
  await expect(page.getByText('声量趋势', { exact: true })).toBeVisible();
  await expect(page.getByText('总声量', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('海底捞', { exact: true }).first()).toBeVisible();

  // 版本选择器：latest_version=2，切到 v1 后受限内容消失。
  const versionSelect = page.getByRole('combobox', { name: '版本选择' });
  await expect(versionSelect).toBeVisible();
  await versionSelect.selectOption('1');
  await expect(page.getByText('品牌整体声量健康', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('数据受限', { exact: true })).toHaveCount(0);
});

// --------------------------------------------------------------------------- //
// 3. 未读圆点：模块出现更新产物后打点，查看后清除
// --------------------------------------------------------------------------- //

test('shows an unread dot on a module with a newer artifact and clears it when seen', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const runId = 'run-unread';
  const brandArt1 = artifactMeta('brand-art', 'brand', 'brand_report_v3', null, 1, 10);
  const brandArt2 = artifactMeta('brand-art-2', 'brand', 'brand_report_v3', null, 1, 20);

  // 目录状态随 Run 产物事件推进：running 阶段只有 v1，publish 后含 v2（更高 sequence）。
  let phase: 'running' | 'published' = 'running';
  const artifacts = () => phase === 'running' ? [brandArt1] : [brandArt1, brandArt2];
  const runEvents = (): string => {
    const base: SseEvent[] = [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
    ];
    if (phase === 'running') return sseBody(runId, base);
    return sseBody(runId, [
      ...base,
      { seq: 3, event: 'artifact.draft.created', payload: { artifact_id: 'brand-art-2', module: 'brand', version: 1, status: 'draft', title: '品牌报告 v2' } },
      { seq: 4, event: 'artifact.published', payload: { artifact_id: 'brand-art-2', module: 'brand' } },
      { seq: 5, event: 'run.completed', payload: { outcome: 'completed' } },
    ]);
  };

  await installArtifactRoutes(page, {
    sessionId: 's-unread',
    sessionTitle: '未读会话',
    artifacts,
    versionPayload: () => brandPayloadComplete(),
    artifactMeta: id => artifactMeta(id, 'brand', 'brand_report_v3', null, 1, 20),
    runId,
    runEvents,
    // 服务端水位：brand 已读到 10（brandArt1 已查看），离线期间的更高 seq 才打点。
    readStates: () => [{ module: 'brand', last_seen_sequence: 10 }],
  });

  await login(page, phone);
  await ensureBiPane(page);

  const brandTab = page.getByRole('tab', { name: '品牌分析' });
  const unreadDot = brandTab.getByTestId('unread-dot');

  // 首屏先进入品牌分析，等 brandArt1（seq 10）渲染完成：服务端水位 10 = 当前
  // 最大 seq，首屏不打点；也避免「首屏产物拉取与 phase 翻转竞态」导致本地水位
  // 被更高 sequence 的 brandArt2 覆盖（点击上报会把水位推到 20 则永不出圆点）。
  await brandTab.click();
  await expect(page.getByText('概览', { exact: true }).first()).toBeVisible();

  // 目录出现更高 sequence（20）的产物 → 圆点出现（20 > 水位 10）。
  phase = 'published';
  await expect(unreadDot).toHaveCount(1);
  // 新产物只提示更新，不得抢占用户当前正在查看的品牌 Tab。
  await expect(brandTab).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: '活动分析' })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: '达人', exact: true })).toHaveAttribute('aria-selected', 'false');

  // 再次点击品牌分析 Tab → 圆点清除。
  await brandTab.click();
  await expect(unreadDot).toHaveCount(0);
});

// --------------------------------------------------------------------------- //
// 4. 子分析挂父产物下
// --------------------------------------------------------------------------- //

test('renders a child insight under its parent artifact', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const brandArt = artifactMeta('brand-art', 'brand', 'brand_report_v3', null, 1, 10);
  const insightArt = artifactMeta('insight-art', 'brand', 'insight_board_v1', 'brand-art', 1, 11);

  await installArtifactRoutes(page, {
    sessionId: 's-insight',
    sessionTitle: '钻取会话',
    artifacts: () => [brandArt, insightArt],
    versionPayload: (artifactId) => artifactId === 'insight-art' ? insightPayload() : brandPayloadComplete(),
    artifactMeta: id => id === 'insight-art'
      ? artifactMeta('insight-art', 'brand', 'insight_board_v1', 'brand-art', 1, 11)
      : brandArt,
  });

  await login(page, phone);
  await ensureBiPane(page);
  await page.getByRole('tab', { name: '品牌分析' }).click();

  // 父产物下出现「钻取分析」子条目，可展开渲染 insight_board_v1。
  await expect(page.getByText('钻取分析', { exact: true })).toBeVisible();
  await expect(page.getByText('情感钻取分析', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '情感钻取分析' }).click();
  await expect(page.getByText('针对品牌情感维度的钻取。', { exact: true })).toBeVisible();
  await expect(page.getByText('整体情感健康。', { exact: true })).toBeVisible();
});

// --------------------------------------------------------------------------- //
// 5. 达人详情：点击圈选列表中的达人打开详情弹窗
// --------------------------------------------------------------------------- //

test('opens a cached KOL detail dialog from the selection list without starting a helper run', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const kolSelArt = artifactMeta('kol-sel-art', 'kol-selection', 'kol_selection_v3', null, 1, 8);

  await installArtifactRoutes(page, {
    sessionId: 's-kol',
    sessionTitle: '达人会话',
    artifacts: () => [kolSelArt],
    versionPayload: (artifactId) => artifactId === 'kol-detail-art'
      ? kolDetailPayload()
      : kolSelectionPayload(),
    artifactMeta: id => id === 'kol-detail-art'
      ? artifactMeta('kol-detail-art', 'kol-detail', 'kol_detail_v2', null, 1, 9)
      : kolSelArt,
  });
  let cachedDetailRequests = 0;
  // 后注册的精确路由覆盖通用 helper-run fixture：命中缓存时后端直接返回 payload，
  // UI 不应再订阅 Run 或拉取另一个 artifact 版本。
  await page.route('**/api/v1/agent/sessions/s-kol/kol-details', route => {
    cachedDetailRequests += 1;
    return route.fulfill({
      status: 201,
      json: { run_id: null, artifact_id: null, cached: true, detail: kolDetailPayload() },
    });
  });

  await login(page, phone);
  await ensureBiPane(page);

  // 达人 Tab → 圈选达人子 Tab。
  await page.getByRole('tab', { name: '达人', exact: true }).click();
  await page.getByRole('tab', { name: '圈选达人' }).click();

  await expect(page.getByText(/按综合评分展示 Top 20/)).toBeVisible();
  const kolCard = page.getByRole('button', { name: /查看美食小A详情/ });
  await expect(kolCard).toBeVisible();
  await expect(page.getByText('美食小A', { exact: true }).first()).toBeVisible();

  // 点击打开详情弹窗：缓存命中 payload 直接渲染，不走 helper Run。
  await kolCard.click();
  const dialog = page.getByRole('dialog', { name: '美食小A达人详情' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('粉丝数', { exact: true })).toBeVisible();
  await expect(dialog.getByText('受众画像', { exact: true })).toBeVisible();
  await expect(dialog.getByText('粉丝趋势', { exact: true })).toBeVisible();
  await expect(dialog.getByText('最新热帖', { exact: true })).toBeVisible();
  await expect(dialog.getByText('上海宝藏美食合集', { exact: true })).toBeVisible();
  expect(cachedDetailRequests).toBe(1);
});

// --------------------------------------------------------------------------- //
// 6. 品牌 Excel 导出：BI 视图导出按钮 → 新 artifact export 路由（按查看版本）
// --------------------------------------------------------------------------- //

test('exports the viewed version of a published artifact via the UI button', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const brandArt = artifactMeta('brand-art', 'brand', 'brand_report_v3', null, 2, 10);

  await installArtifactRoutes(page, {
    sessionId: 's-export',
    sessionTitle: '导出会话',
    artifacts: () => [brandArt],
    versionPayload: (artifactId, version) => version === 2 ? brandPayloadRestricted() : brandPayloadComplete(),
    artifactMeta: () => brandArt,
  });
  // 导出路由单独覆盖（后注册优先于 installArtifactRoutes 的通配），记录查询参数
  // 以断言「导出版本 = 界面当前查看版本」。
  let exportSearch = '';
  await page.route('**/api/v1/agent/artifacts/brand-art/export*', route => {
    exportSearch = new URL(route.request().url()).search;
    return route.fulfill({
      status: 200,
      body: Buffer.from('xlsx'),
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(EXPORT_FILENAME)}`,
      },
    });
  });

  await login(page, phone);
  await ensureBiPane(page);

  await page.getByRole('tab', { name: '品牌分析' }).click();
  await expect(page.getByText('概览', { exact: true }).first()).toBeVisible();

  // 切到历史版本 v1，再点「导出 Excel」：导出与界面查看版本一致（version=1）。
  await page.getByRole('combobox', { name: '版本选择' }).selectOption('1');
  await expect(page.getByText('品牌整体声量健康').first()).toBeVisible();

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 Excel' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(EXPORT_FILENAME);
  expect(exportSearch).toContain('version=1');
});

// --------------------------------------------------------------------------- //
// 7. 活动、达人 Excel 导出：三个可导出模块均使用当前 published version
// --------------------------------------------------------------------------- //

test('exports published campaign and KOL artifacts through their fixed BI tabs', async ({ page }) => {
  const phone = `138${Date.now().toString().slice(-8)}`;
  const campaignArt = artifactMeta('campaign-art', 'campaign', 'campaign_report_v2', null, 1, 12);
  const kolArt = artifactMeta('kol-art', 'kol-selection', 'kol_selection_v3', null, 1, 13);
  const exportPaths: string[] = [];

  await installArtifactRoutes(page, {
    sessionId: 's-export-other-modules',
    sessionTitle: '多模块导出会话',
    artifacts: () => [campaignArt, kolArt],
    // 活动 payload 无需参与本用例的视觉渲染；导出能力由已发布类型与版本决定。
    versionPayload: artifactId => artifactId === 'kol-art' ? kolSelectionPayload() : {},
    artifactMeta: artifactId => artifactId === 'campaign-art' ? campaignArt : kolArt,
  });
  const fulfillExport = (route: import('@playwright/test').Route) => {
    exportPaths.push(new URL(route.request().url()).pathname + new URL(route.request().url()).search);
    return route.fulfill({
      status: 200,
      body: Buffer.from('xlsx'),
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(EXPORT_FILENAME)}`,
      },
    });
  };
  await page.route('**/api/v1/agent/artifacts/campaign-art/export*', fulfillExport);
  await page.route('**/api/v1/agent/artifacts/kol-art/export*', fulfillExport);

  await login(page, phone);
  await ensureBiPane(page);

  await page.getByRole('tab', { name: '活动分析' }).click();
  await expect(page.getByRole('button', { name: '导出 Excel' })).toBeVisible();
  let downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 Excel' }).click();
  expect((await downloadPromise).suggestedFilename()).toBe(EXPORT_FILENAME);

  await page.getByRole('tab', { name: '达人', exact: true }).click();
  await page.getByRole('tab', { name: '圈选达人' }).click();
  await expect(page.getByRole('button', { name: '导出 Excel' })).toBeVisible();
  downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 Excel' }).click();
  expect((await downloadPromise).suggestedFilename()).toBe(EXPORT_FILENAME);

  expect(exportPaths).toEqual([
    '/api/v1/agent/artifacts/campaign-art/export?version=1',
    '/api/v1/agent/artifacts/kol-art/export?version=1',
  ]);
});
