import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import * as tasksApi from '../api/tasks';
import { candidatePage, emptyAnalytics, missingDataReport, reportFixture } from '../test/fixtures';
import BiReport from './BiReport';

const emptyBrandAnalytics = { ...emptyAnalytics };

describe('BiReport', () => {
  it('renders the nine KOL decision sections from a matching report', () => {
    render(<BiReport report={reportFixture({ candidate_version: 3 })} candidateVersion={3} selectedCandidates={candidatePage.items} />);

    for (const title of [
      '任务概览', '评分构成', '受众与内容匹配', '平台分布', '预算与性价比',
      '候选对比', '风险与数据质量', 'AI 结论', '数据来源',
    ]) {
      expect(screen.getByText(title)).toBeVisible();
    }
  });

  it('does not render a report for another candidate version', () => {
    render(<BiReport report={reportFixture({ candidate_version: 2 })} candidateVersion={3} />);

    expect(screen.getByText('正在同步最新候选与 BI 报告')).toBeVisible();
    expect(screen.queryByText('AI 结论')).not.toBeInTheDocument();
  });

  it('labels missing data instead of showing zero', () => {
    render(<BiReport report={missingDataReport} candidateVersion={2} />);

    expect(screen.getByText('受众数据不足')).toBeVisible();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });

  it('switches the right BI panel to the latest-round data analysis tab', () => {
    render(<BiReport report={reportFixture({ candidate_version: 2 })} candidateVersion={2} taskStatus="completed" />);

    const analyticsTab = screen.getByRole('tab', { name: '数据分析' });
    expect(analyticsTab).toHaveAttribute('aria-selected', 'false');
    fireEvent.click(analyticsTab);
    expect(analyticsTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('region', { name: '数据分析' })).toBeVisible();
    expect(screen.getByText('舆情情感极性分析')).toBeVisible();
    expect(screen.queryByText('任务概览')).not.toBeInTheDocument();
  });

  it('renders a brand-only report without requiring a candidate list', () => {
    render(
      <BiReport
        report={reportFixture({
          candidate_version: 0,
          analysis_scope: 'brand',
          brand_analytics: {
            ...emptyBrandAnalytics,
            volume_trend: [
              { period: '2026-05', value: 10, unit: '条', platforms: ['xiaohongshu'] },
              { period: '2026-06', value: 20, unit: '条', platforms: ['douyin'] },
            ],
            sentiment_trend: [
              { period: '2026-05', value: 0.7, unit: '指数', platforms: ['xiaohongshu'] },
              { period: '2026-06', value: 0.8, unit: '指数', platforms: ['douyin'] },
            ],
          },
        })}
        taskStatus="completed"
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: '数据分析' }));
    expect(screen.getByText('品牌声量变化趋势')).toBeVisible();
    expect(screen.getByText('用户情感趋势')).toBeVisible();
  });

  it('uses the report average score, platform count, and same-version candidates', () => {
    const report = reportFixture({
      score_composition: [{ dimension: 'audience', average: 82 }],
      platform_distribution: [{ platform: 'bilibili', count: 2 }],
      comparison: [{ nickname: '报告达人', total_score: 91 }],
    });

    const { rerender } = render(
      <BiReport report={report} candidateVersion={2} selectedCandidates={candidatePage.items} selectedCandidateVersion={1} />,
    );

    expect(screen.getByText('报告达人')).toBeVisible();
    expect(screen.getByText('2 位')).toBeVisible();
    expect(screen.getByLabelText('评分构成图表：受众匹配 82')).toBeVisible();

    rerender(<BiReport report={report} candidateVersion={2} selectedCandidates={candidatePage.items} selectedCandidateVersion={2} />);
    expect(screen.getByText('#1 达人甲')).toBeVisible();
  });

  it('shows only source display fields delivered by the report DTO', () => {
    render(<BiReport report={reportFixture({ sources: [{ tool_name_cn: 'B站数据采集', collected_at: '2026-07-15T10:00:00Z', evidence_id: 'evidence-1', internal_endpoint: '/secret' }] })} candidateVersion={2} />);

    expect(screen.getByText('B站数据采集')).toBeVisible();
    expect(screen.getByText(/证据编号：evidence-1/)).toBeVisible();
    expect(screen.queryByText('/secret')).not.toBeInTheDocument();
  });

  it('disables Excel export while the latest task is running', () => {
    render(<BiReport report={reportFixture({ candidate_version: 2 })} candidateVersion={2} sessionId="session-1" taskStatus="running" />);

    expect(screen.getByRole('button', { name: '导出 Excel' })).toBeDisabled();
  });

  it('downloads the latest session workbook after completion', async () => {
    const download = vi.spyOn(tasksApi, 'downloadLatestSessionExport').mockResolvedValue({ blob: new Blob(['xlsx']), filename: '测试.xlsx' });
    const createObjectURL = vi.fn(() => 'blob:test');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });

    render(<BiReport report={reportFixture({ candidate_version: 2 })} candidateVersion={2} sessionId="session-1" taskStatus="completed" hasCandidateData />);
    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));

    await waitFor(() => expect(download).toHaveBeenCalledWith('session-1'));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
