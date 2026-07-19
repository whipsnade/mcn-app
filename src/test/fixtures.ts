import type { ApiAnalysisReport } from '../api/contracts';

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
