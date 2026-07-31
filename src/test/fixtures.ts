import type { ApiAnalysisReport, BrandReportPayload } from '../api/contracts';

export function analysisReportFixture(overrides: Partial<ApiAnalysisReport> = {}): ApiAnalysisReport {
  return {
    id: 'analysis-report-1',
    task_id: 'task-1',
    version: 1,
    title: '品牌自由分析报告',
    blocks: [
      { type: 'heading', text: '一、核心结论' },
      { type: 'markdown', text: '本次 campaign 整体声量向好。\n建议继续关注小红书渠道。' },
      {
        type: 'metric_grid',
        title: '核心指标',
        items: [
          { label: '总曝光量', value: 12500000, unit: '次', delta: '+37.2%' },
          { label: '互动表现', value: '高于大盘', delta: '-2.1%' },
        ],
      },
      {
        type: 'table',
        title: '平台表现',
        columns: ['平台', '声量', '互动率'],
        rows: [['小红书', 15745, '6.2%'], ['抖音', 9021, null]],
      },
      {
        type: 'bar_chart',
        title: '平台声量对比',
        categories: ['小红书', '抖音'],
        series: [{ name: '声量', values: [15745, 9021] }, { name: '互动量', values: [980, 1204] }],
      },
      {
        type: 'line_chart',
        title: '曝光走势',
        categories: ['06-20', '06-22', '06-26'],
        series: [{ name: '曝光量', values: [100000, 4200000, 700000] }],
      },
      {
        type: 'pie_chart',
        title: '情感占比',
        categories: ['正向', '中立', '负向'],
        series: [{ name: '占比', values: [78, 15, 7] }],
      },
      { type: 'tag_list', title: '高热词', items: ['质感高级', '绝美雾面', '回购'] },
      {
        type: 'sources',
        items: [{ name: '品牌声量查询', collected_at: '2026-07-15T10:00:00Z', evidence: 'EV-001' }],
      },
    ],
    conclusion: '整体表现优于预期，建议加大小红书投放。',
    status: 'completed',
    generated_at: '2026-07-15T10:00:00Z',
    ...overrides,
  };
}

/** brand_report_v2 完整快照：8 章节全 complete、双平台热帖、含叙事层。 */
export function brandReportPayloadFixture(overrides: Partial<BrandReportPayload> = {}): BrandReportPayload {
  return {
    template_version: 'brand_report_v2',
    data_status: 'complete',
    scope: {
      brand: '海底捞',
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
          { platform: 'xiaohongshu', mentions: 1200, interactions: 56000 },
          { platform: 'douyin', mentions: 800, interactions: 91000 },
        ],
        total_mentions: {
          current: 2000,
          mom: { value: 1800, status: 'ok', reason: null },
          yoy: { value: 1500, status: 'ok', reason: null },
          mom_change_pct: 11.1,
          yoy_change_pct: 33.3,
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
          { platform: 'xiaohongshu', sentiment: '负面', mentions: 60, interactions: 3000, share_pct: 5 },
          { platform: 'douyin', sentiment: '正面', mentions: 600, interactions: 70000, share_pct: 75 },
        ],
      },
      daily_trend: {
        points: [
          { date: '2026-06-01', mentions: 60, interactions: 3000 },
          { date: '2026-06-15', mentions: 320, interactions: 21000 },
          { date: '2026-06-30', mentions: 90, interactions: 5000 },
        ],
        peak_date: '2026-06-15',
        peak_mentions: 320,
      },
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
          title: '海底捞隐藏吃法合集，最后一种真的绝了一定要试试看再推荐给朋友们',
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
      daily_trend: { status: 'complete', missing_fields: [], reason: null, source_tools: ['brand_trend_query'], collected_at: null },
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
    ...overrides,
  };
}
