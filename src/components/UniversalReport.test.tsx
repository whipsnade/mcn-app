import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { downloadKolSelection, runKolAnalysis } from '../api/kolSelection';
import { analysisReportFixture } from '../test/fixtures';
import UniversalReport from './UniversalReport';

vi.mock('../api/kolSelection', () => ({
  runKolAnalysis: vi.fn(),
  downloadKolSelection: vi.fn(),
}));

describe('UniversalReport', () => {
  beforeEach(() => {
    vi.mocked(runKolAnalysis).mockReset();
    vi.mocked(downloadKolSelection).mockReset();
  });

  it('renders every supported block type from the analysis report DTO', () => {
    render(<UniversalReport report={analysisReportFixture()} taskStatus="completed" />);

    expect(screen.getByText('品牌自由分析报告')).toBeVisible();
    // heading 与 markdown
    expect(screen.getByText('一、核心结论')).toBeVisible();
    expect(screen.getByText(/本次 campaign 整体声量向好/)).toBeVisible();
    // metric_grid：数字与字符串取值、delta
    expect(screen.getByText('总曝光量')).toBeVisible();
    expect(screen.getByText('12,500,000')).toBeVisible();
    expect(screen.getByText('高于大盘')).toBeVisible();
    expect(screen.getByText('+37.2%')).toBeVisible();
    // table：表头、单元格与 null 降级
    expect(screen.getByText('平台表现')).toBeVisible();
    expect(screen.getByText('互动率')).toBeVisible();
    expect(screen.getByText('9021')).toBeVisible();
    expect(screen.getByText('—')).toBeVisible();
    // 图表卡片标题
    expect(screen.getByText('平台声量对比')).toBeVisible();
    expect(screen.getByText('曝光走势')).toBeVisible();
    expect(screen.getByText('情感占比')).toBeVisible();
    // tag_list 与 sources
    expect(screen.getByText('质感高级')).toBeVisible();
    expect(screen.getByText('品牌声量查询')).toBeVisible();
    expect(screen.getByText(/证据编号：EV-001/)).toBeVisible();
    // 结论
    expect(screen.getByText('整体表现优于预期，建议加大小红书投放。')).toBeVisible();
  });

  it('skips blocks whose payload is empty or incomplete', () => {
    render(
      <UniversalReport
        report={analysisReportFixture({
          blocks: [
            { type: 'heading', text: '  ' },
            { type: 'markdown', text: '' },
            { type: 'metric_grid', title: '空指标', items: [] },
            { type: 'table', title: '空表格', columns: ['平台'], rows: [] },
            { type: 'bar_chart', title: '空柱状', categories: [], series: [{ name: '声量', values: [1] }] },
            { type: 'line_chart', title: '空折线', categories: ['06-20'], series: [{ name: '曝光', values: [1] }] },
            { type: 'pie_chart', title: '空饼图', categories: ['正向'], series: [{ name: '占比', values: [null] }] },
            { type: 'tag_list', title: '空热词', items: [] },
            { type: 'sources', items: [] },
          ],
          conclusion: null,
        })}
        taskStatus="completed"
      />,
    );

    for (const title of ['空指标', '空表格', '空柱状', '空折线', '空饼图', '空热词', '数据来源', 'AI 结论']) {
      expect(screen.queryByText(title)).not.toBeInTheDocument();
    }
    expect(screen.getByText('报告内容为空')).toBeVisible();
  });

  it('announces that report content may still change while the task runs', () => {
    render(<UniversalReport report={analysisReportFixture()} taskStatus="running" />);

    expect(screen.getByRole('status')).toHaveTextContent('任务进行中，报告内容可能继续更新');
    expect(screen.getByText('一、核心结论')).toBeVisible();
  });

  it('shows the selection count prompt in the empty state', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={12} />);

    expect(screen.getByText('已圈选 12 位达人，点击「分析」生成 KOL 分析报告')).toBeVisible();
  });

  it('asks for a selection first when nothing has been selected', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    expect(screen.getByText(/尚未圈选达人/)).toBeVisible();
  });

  it('renders the analyze and export buttons when a session is bound', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    expect(screen.getByRole('button', { name: '分析' })).toBeVisible();
    expect(screen.getByRole('button', { name: '导出 Excel' })).toBeVisible();
  });

  it('hides the action buttons without a session id', () => {
    render(<UniversalReport report={analysisReportFixture()} taskStatus="completed" />);

    expect(screen.queryByRole('button', { name: '分析' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '导出 Excel' })).not.toBeInTheDocument();
  });

  it('runs the manual analysis and forwards the report to the callback', async () => {
    const report = analysisReportFixture({ id: 'analysis-report-kol', task_id: null, title: 'KOL 匹配度分析' });
    vi.mocked(runKolAnalysis).mockResolvedValue(report);
    const onReportReady = vi.fn();
    render(<UniversalReport sessionId="session-1" selectionCount={3} onReportReady={onReportReady} />);

    fireEvent.click(screen.getByRole('button', { name: '分析' }));

    expect(runKolAnalysis).toHaveBeenCalledWith('session-1');
    await waitFor(() => expect(onReportReady).toHaveBeenCalledWith(report));
  });

  it('shows an inline error when there is no KOL selection', async () => {
    vi.mocked(runKolAnalysis).mockRejectedValue(new Error('NO_KOL_SELECTION'));
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('button', { name: '分析' }));

    expect(await screen.findByText(/暂无圈选达人/)).toBeVisible();
  });

  it('downloads the KOL selection sheet on export', async () => {
    vi.mocked(downloadKolSelection).mockResolvedValue(undefined);
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));

    await waitFor(() => expect(downloadKolSelection).toHaveBeenCalledWith('session-1'));
  });

  it('keeps a placeholder card for the upcoming brand/campaign analysis', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    expect(screen.getByText(/品牌\/活动分析（即将上线）/)).toBeVisible();
  });
});
