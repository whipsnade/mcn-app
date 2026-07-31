import { expect, test } from '@playwright/test';


// 品牌含文件名非法字符（/ : * ? " < > |），后端导出会按 sanitize_report_filename 剔除。
// sanitize 规则本身由后端单测覆盖，此处仅验证前端对 Content-Disposition 的解码还原：
// mock 直接给清洗后的文件名，断言 suggestedFilename 与之一致。
const EXPORT_FILENAME = '海底捞火锅2026_品牌社媒分析报告_2026-06-01-2026-06-30_v2.xlsx';

// brand_report_v2 简化快照（形状参照 src/test/fixtures.ts 的 brandReportPayloadFixture）：
// data_status=partial，趋势章节 unavailable（no_data），必须过 BrandReportView 的 isBrandReportPayload 守卫。
const brandPayloadV2 = {
  template_version: 'brand_report_v2',
  data_status: 'partial',
  scope: {
    brand: '海/底:捞*?"火锅"<2026>|',
    period_start: '2026-06-01',
    period_end: '2026-06-30',
    platforms: ['xiaohongshu', 'douyin'],
    comparison_mode: 'mom_yoy',
    data_as_of: null,
  },
  query_spec: {
    original_term: '海底捞',
    matched_tag: '海底捞火锅',
    fallback_keyword: null,
    comparison_definition: '环比 2026-05，同比 2025-06',
  },
  data: {
    overview: {
      platforms: [
        { platform: 'xiaohongshu', mentions: 1200, exposure: 3400000, interactions: 56000 },
        { platform: 'douyin', mentions: 800, exposure: 5200000, interactions: 91000 },
      ],
      total_mentions: {
        current: 2000,
        mom: { value: 1800, status: 'ok', reason: null },
        yoy: { value: 1500, status: 'ok', reason: null },
        mom_change_pct: 11.1,
        yoy_change_pct: 33.3,
      },
      total_exposure: {
        current: 8600000,
        mom: { value: 7000000, status: 'ok', reason: null },
        yoy: { value: null, status: 'not_requested', reason: null },
        mom_change_pct: 22.9,
        yoy_change_pct: null,
      },
      total_interactions: {
        current: 147000,
        mom: { value: 160000, status: 'ok', reason: null },
        yoy: { value: 120000, status: 'ok', reason: null },
        mom_change_pct: -8.1,
        yoy_change_pct: 22.5,
      },
      sentiment_split: { positive: 1500, neutral: 400, negative: 100 },
    },
    sentiment: {
      rows: [
        { platform: 'xiaohongshu', sentiment: '正面', mentions: 900, interactions: 40000, share_pct: 75 },
        { platform: 'douyin', sentiment: '负面', mentions: 80, interactions: 5000, share_pct: 10 },
      ],
    },
    daily_trend: { points: [], peak_date: null, peak_mentions: null },
    content_types: [
      { content_type: '图文', mentions: 1100, share_pct: 55 },
      { content_type: '视频', mentions: 900, share_pct: 45 },
    ],
    creator_tiers: [
      { tier: '头部', mentions: 500, share_pct: 25 },
      { tier: '腰部', mentions: 900, share_pct: 45 },
    ],
    organic_vs_paid: {
      organic_mentions: 1500,
      paid_mentions: 500,
      organic_share_pct: 75,
      paid_share_pct: 25,
    },
    regions: [
      { region: '广东', mentions: 400, interactions: 20000, share_pct: 20 },
      { region: '上海', mentions: 300, interactions: 15000, share_pct: 15 },
    ],
    top_posts: [
      {
        platform: 'xiaohongshu',
        post_id: 'xhs-1',
        collected_at: '2026-06-20T10:00:00Z',
        title: '海底捞隐藏吃法合集',
        author: '吃货小分队',
        interactions: 12000,
        exposure_count: 300000,
        like_count: 8000,
        comment_count: 900,
        collect_count: 2500,
        share_count: 600,
        sentiment: '正面',
        creator_tier: '腰部',
        url: 'https://www.xiaohongshu.com/explore/xhs-1',
      },
      {
        platform: 'douyin',
        post_id: 'dy-1',
        collected_at: '2026-06-18T09:00:00Z',
        title: '海底捞生日歌现场太上头了',
        author: '快乐记录仪',
        interactions: 45000,
        exposure_count: 1200000,
        like_count: 30000,
        comment_count: 4000,
        collect_count: null,
        share_count: 8000,
        sentiment: '正面',
        creator_tier: '头部',
        url: 'https://www.douyin.com/video/dy-1',
      },
    ],
  },
  narrative: {
    praise_points: ['服务体验好评集中'],
    complaint_points: ['排队时间过长'],
    impact_level: '中',
    expansion_signals: ['海外门店讨论升温'],
    noise_notes: null,
    key_findings: ['正面声量占主导'],
    conclusion: '品牌整体声量健康，负面集中于排队体验。',
    recommendations: ['优化排队体验', '加大抖音投放'],
  },
  availability: {
    overview: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_mention_query'], collected_at: null },
    sentiment: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_mention_query'], collected_at: null },
    daily_trend: { status: 'unavailable', missing_fields: ['points'], reason: 'no_data', source_tools: ['brand_trend_query'], collected_at: null },
    content_creators: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_mention_query'], collected_at: null },
    regions: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_audience_query'], collected_at: null },
    top_posts: { status: 'complete', missing_fields: [], reason: null, source_tools: ['top_posts_query'], collected_at: null },
    insights: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_mention_query'], collected_at: null },
    methodology: { status: 'complete', missing_fields: [], reason: null, source_tools: [], collected_at: null },
  },
  sources: [
    { tool: 'brand_mention_query', collected_at: '2026-07-01T10:00:00Z', step_id: 'step_1' },
    { tool: 'brand_trend_query', collected_at: null, step_id: 'step_2' },
  ],
};

const reportV2 = {
  id: 'brand-report-v2', task_id: 'task-brand', report_type: 'brand_analysis',
  scope: { brand: '海底捞' },
  version: 2, title: '海底捞品牌社媒分析报告', conclusion: null,
  // 真实 v2 报告必落非空兼容 blocks（旧渲染路径降级用），mock 保持一致。
  blocks: [{ type: 'markdown', text: '兼容旧渲染的降级块。' }],
  status: 'completed', generated_at: '2026-07-30T10:00:00Z',
  template_version: 'brand_report_v2', payload: brandPayloadV2,
};

// 旧版报告：无 payload（template_version 为 null），走旧 Block 渲染 + 不支持模板导出。
const reportV1 = {
  id: 'brand-report-v1', task_id: 'task-brand', report_type: 'brand_analysis',
  scope: { brand: '海底捞' },
  version: 1, title: '海底捞品牌分析v1', conclusion: '旧版结论：声量稳步增长。',
  status: 'completed', generated_at: '2026-07-24T10:00:00Z',
  template_version: null, payload: null,
  blocks: [
    { type: 'heading', text: '一、核心结论' },
    { type: 'markdown', text: '旧版品牌声量整体向好。' },
  ],
};

const reportVersions = [
  { report_id: 'brand-report-v2', title: '海底捞品牌社媒分析报告', version: 2, scope: { brand: '海底捞' }, status: 'completed', created_at: '2026-07-30T10:00:00Z' },
  { report_id: 'brand-report-v1', title: '海底捞品牌分析v1', version: 1, scope: { brand: '海底捞' }, status: 'completed', created_at: '2026-07-24T10:00:00Z' },
];


test('brand report switches versions, shows restricted chapters and downloads the export', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8);
  const session = {
    id: 'session-brand', title: '海底捞品牌分析', brand: '海底捞', campaign_name: null,
    status: 'completed', platforms: ['xiaohongshu', 'douyin'], category: '餐饮', target_audience: '',
    budget_min: null, budget_max: null, filters: {}, is_starred: false, kol_selection_count: 0, messages: [],
    latest_task: { id: 'task-brand', status: 'completed', kind: 'agent', completed_at: '2026-07-30T10:00:00Z' },
    latest_candidates: null,
    latest_report: null,
    latest_analysis_report: null,
    created_at: '2026-07-30T10:00:00Z', updated_at: '2026-07-30T10:00:00Z',
  };
  const artifactsSummary = {
    brand: {
      latest_artifact: {
        artifact_id: 'artifact-brand-v2', artifact_type: 'brand_report',
        title: '海底捞品牌社媒分析报告', version: 2, scope: { brand: '海底捞' },
        status: 'completed', created_at: '2026-07-30T10:00:00Z',
      },
      unread: true,
    },
    campaign: { latest_artifact: null, unread: false },
    kol_analysis: { latest_artifact: null, unread: false },
    kol_selection: { latest_artifact: null, unread: false },
  };

  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(`138${suffix}`);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();

  await page.route('**/api/v1/sessions/session-brand', route => route.fulfill({ json: session }));
  await page.route('**/api/v1/sessions', route => route.fulfill({ json: [session] }));
  await page.route('**/api/v1/sessions/session-brand/artifacts/summary', route => route.fulfill({ json: artifactsSummary }));
  await page.route('**/api/v1/sessions/session-brand/artifact-read-state', route => route.fulfill({ status: 200, json: {} }));
  await page.route(
    url => url.pathname === '/api/v1/sessions/session-brand/reports'
      && url.searchParams.get('report_type') === 'brand_analysis',
    route => route.fulfill({ json: reportVersions }),
  );
  await page.route(
    url => url.pathname === '/api/v1/sessions/session-brand/reports/brand-report-v2/export',
    route => route.fulfill({
      body: Buffer.from('xlsx'),
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(EXPORT_FILENAME)}`,
      },
    }),
  );
  await page.route('**/api/v1/analysis-reports/brand-report-v2', route => route.fulfill({ json: reportV2 }));
  await page.route('**/api/v1/analysis-reports/brand-report-v1', route => route.fulfill({ json: reportV1 }));

  await page.reload();
  const mobileNavigation = page.getByRole('navigation', { name: '移动工作区导航' });
  // 移动导航 xl 断点（1280px）以下才可见：按视口判定后确定性等待其出现再点击，
  // 避免 reload 后渲染未完的非等待探测竞态。
  if ((page.viewportSize()?.width ?? 1440) < 1280) {
    await expect(mobileNavigation).toBeVisible();
    await mobileNavigation.getByRole('button', { name: '分析报告' }).click();
  }

  // 品牌分析 Tab：章节导航 + 8 章节渲染，partial 报告带「数据受限」徽标。
  await page.getByRole('tab', { name: '品牌分析' }).click();
  const chapterNavigation = page.getByRole('navigation', { name: '报告章节' });
  await expect(chapterNavigation).toBeVisible();
  await expect(page.getByText('数据受限', { exact: true }).first()).toBeVisible();
  const chapterLabels = ['概览', '情感', '趋势', '内容与达人', '地域', '热帖', '舆情', '方法论'];
  for (const label of chapterLabels) {
    await expect(chapterNavigation.getByRole('button', { name: label, exact: true })).toBeVisible();
  }
  await expect(page.locator('[data-chapter]')).toHaveCount(8);
  // 趋势章节受限：受限徽标 + 原因说明（no_data → 查询无数据）。
  const trendChapter = page.locator('[data-chapter="daily_trend"]');
  await expect(trendChapter.getByText('受限', { exact: true })).toBeVisible();
  await expect(trendChapter.getByText('数据受限：查询无数据（缺失字段：points）', { exact: true })).toBeVisible();

  // 切到旧版 v1：降级旧 Block 渲染，出现「不支持模板导出」提示且无导出按钮。
  await page.getByRole('combobox', { name: '报告版本' }).selectOption('brand-report-v1');
  await expect(page.getByText('该历史版本不支持模板导出', { exact: true })).toBeVisible();
  await expect(page.getByText('一、核心结论', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '导出报告' })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '报告章节' })).toHaveCount(0);

  // 切回新版 v2：点击导出触发下载，文件名符合清洗规则。
  await page.getByRole('combobox', { name: '报告版本' }).selectOption('brand-report-v2');
  await expect(page.getByRole('navigation', { name: '报告章节' })).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出报告' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(EXPORT_FILENAME);

  await page.screenshot({ path: 'output/playwright/brand-report-export.png', fullPage: true });
});
