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
});
