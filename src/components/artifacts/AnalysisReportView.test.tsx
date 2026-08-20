import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type {
  AnalysisReportBlock,
  AnalysisReportPayload,
} from '../../api/agentArtifacts';
import AnalysisReportView from './AnalysisReportView';

const methodology = {
  data_as_of: '2026-08-21T10:00:00',
  source_names: ['DataTap'],
  notes: ['按真实返回结果展示'],
};

const rows: Array<Array<string | number | null>> = Array.from(
  { length: 45 },
  (_, index) => [`平台-${index + 1}`, index + 1, index === 44 ? null : `https://example.com/${index + 1}`],
);

const report: AnalysisReportPayload = {
  schema_version: 'analysis_report_v1',
  module: 'report',
  data_status: 'restricted',
  availability: {
    blocks: { status: 'partial', reason_codes: ['some_rows_missing'] },
    fulfillment: { status: 'partial', reason_codes: ['long_tail_partial'] },
  },
  limitations: [{
    code: 'long_tail_partial',
    message: '部分长尾数据未返回',
    affected_paths: ['fulfillment'],
  }],
  methodology,
  title: '跨平台营销报告',
  subject_type: 'mixed',
  scope: { brand: '测试品牌', platforms: ['xiaohongshu', 'douyin'] },
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
        { key: 'url', label: '来源', type: 'url' },
      ],
      rows,
    },
    {
      block_type: 'time_series',
      id: 'trend',
      title: '趋势',
      points: [{ timestamp: '2026-08-01', values: { volume: null, engagement: 12 } }],
    },
    {
      block_type: 'link_list',
      id: 'links',
      title: '参考链接',
      items: [
        { label: '官网', url: 'https://example.com/report', description: '安全链接' },
        { label: '不安全链接', url: 'javascript:alert(1)', description: null },
      ],
    },
    {
      block_type: 'chart',
      id: 'chart',
      title: '平台趋势图',
      chart_type: 'line',
      categories: ['小红书', '抖音'],
      series: [{ key: 'volume', label: '声量', values: [10, null] }],
    },
    {
      block_type: 'narrative',
      id: 'summary',
      title: '报告摘要',
      content: '报告摘要内容',
      supporting_paths: ['/blocks/5'],
    },
    {
      block_type: 'methodology_limitations',
      id: 'method',
      title: '方法与限制',
      methodology: '按平台聚合真实返回数据',
      limitations: ['部分长尾数据未返回'],
    },
  ],
  fulfillment: [{
    key: 'requested_items',
    requested_min: 50,
    actual_count: 45,
    status: 'partial',
    reason: '上游只返回 45 条',
  }],
  workbook: null,
};

describe('AnalysisReportView', () => {
  it('renders every generic block, keeps all rows, and marks null/unsafe values as restricted', () => {
    render(<AnalysisReportView payload={report} />);

    expect(screen.getByRole('heading', { name: '跨平台营销报告' })).toBeVisible();
    expect(screen.getAllByText('数据受限').length).toBeGreaterThan(0);
    expect(screen.getByText('平台-1')).toBeVisible();
    expect(screen.getByText('平台-45')).toBeVisible();
    expect(screen.getByRole('link', { name: '官网' })).toHaveAttribute('href', 'https://example.com/report');
    expect(screen.getByText('不安全链接')).toBeVisible();
    expect(screen.getByText('链接不可用')).toBeVisible();
    expect(screen.getByText('报告摘要内容')).toBeVisible();
    expect(screen.getByText('数据来源')).toBeVisible();
    expect(screen.getByText('DataTap')).toBeVisible();
    expect(screen.getByText('按真实返回结果展示')).toBeVisible();
    expect(screen.getByText('按平台聚合真实返回数据')).toBeVisible();
    expect(screen.getAllByText('部分长尾数据未返回').length).toBeGreaterThan(0);
  });

  it('uses a controlled fallback for an unknown future block', () => {
    const futureBlock = {
      block_type: 'future_block',
      id: 'future',
      title: '未来模块',
    } as unknown as AnalysisReportBlock;
    render(<AnalysisReportView payload={{ ...report, blocks: [...report.blocks, futureBlock] }} />);

    expect(screen.getByText('未来模块')).toBeVisible();
    expect(screen.getByText('暂不支持的报告模块')).toBeVisible();
  });
});
