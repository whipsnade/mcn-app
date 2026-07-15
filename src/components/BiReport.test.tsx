import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { candidatePage, missingDataReport, reportFixture } from '../test/fixtures';
import BiReport from './BiReport';

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

  it('uses the report average score, platform count, and same-version candidates', () => {
    const report = reportFixture({
      score_composition: [{ dimension: 'audience', average: 82 }],
      platform_distribution: [{ platform: 'bilibili', count: 2 }],
      comparison: [{ platform_account_id: '报告达人', total_score: 91 }],
    });

    const { rerender } = render(
      <BiReport report={report} candidateVersion={2} selectedCandidates={candidatePage.items} selectedCandidateVersion={1} />,
    );

    expect(screen.getByText('报告达人')).toBeVisible();
    expect(screen.getByText('2 位')).toBeVisible();
    expect(screen.getByLabelText('评分构成图表：audience 82')).toBeVisible();

    rerender(<BiReport report={report} candidateVersion={2} selectedCandidates={candidatePage.items} selectedCandidateVersion={2} />);
    expect(screen.getByText('#1 达人甲')).toBeVisible();
  });

  it('shows only source display fields delivered by the report DTO', () => {
    render(<BiReport report={reportFixture({ sources: [{ tool_name_cn: 'B站数据采集', collected_at: '2026-07-15T10:00:00Z', evidence_id: 'evidence-1', internal_endpoint: '/secret' }] })} candidateVersion={2} />);

    expect(screen.getByText('B站数据采集')).toBeVisible();
    expect(screen.getByText(/证据编号：evidence-1/)).toBeVisible();
    expect(screen.queryByText('/secret')).not.toBeInTheDocument();
  });
});
