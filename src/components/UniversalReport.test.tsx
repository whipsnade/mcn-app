import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { downloadKolSelection, getKolSelection, runKolAnalysis } from '../api/kolSelection';
import { analysisReportFixture } from '../test/fixtures';
import UniversalReport from './UniversalReport';

vi.mock('../api/kolSelection', () => ({
  runKolAnalysis: vi.fn(),
  downloadKolSelection: vi.fn(),
  getKolSelection: vi.fn(),
}));

function kolSelectionItem(overrides: Record<string, unknown> = {}) {
  return {
    platform: 'xiaohongshu',
    kol_uid: 'uid-1',
    nickname: '达人小A',
    followers: 120000,
    city: '上海',
    profile_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
    fields: { export_fields: { engagement_rate: 5.2, quoted_price_cny: 12000 } },
    score: { total: 82, rating: '重点推荐', stars: '★★★★★', dimensions: {}, data_completeness: 0.9 },
    ...overrides,
  };
}

describe('UniversalReport', () => {
  beforeEach(() => {
    vi.mocked(runKolAnalysis).mockReset();
    vi.mocked(downloadKolSelection).mockReset();
    vi.mocked(getKolSelection).mockReset();
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

  it('shows an inline error when the export fails', async () => {
    vi.mocked(downloadKolSelection).mockRejectedValue(new Error('HTTP_500'));
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('导出失败，请稍后重试');
  });

  it('maps a report version conflict to a friendly message', async () => {
    vi.mocked(runKolAnalysis).mockRejectedValue(new Error('REPORT_VERSION_CONFLICT'));
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    fireEvent.click(screen.getByRole('button', { name: '分析' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('报告生成中，请稍后刷新查看');
  });

  it('no longer renders the brand/campaign placeholder card', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    expect(screen.queryByText(/品牌\/活动分析/)).not.toBeInTheDocument();
  });

  it('renders the report and selection tabs with the selection count', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={12} />);

    expect(screen.getByRole('tab', { name: 'KOL 分析' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '圈选达人 (12)' })).toHaveAttribute('aria-selected', 'false');
  });

  it('keeps the report view as the default tab content', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    expect(screen.getByText(/已圈选 3 位达人/)).toBeVisible();
    expect(getKolSelection).not.toHaveBeenCalled();
  });

  it('loads and renders KOL cards when switching to the selection tab', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));

    await waitFor(() => expect(getKolSelection).toHaveBeenCalledWith('session-1'));
    expect(await screen.findByText('达人小A')).toBeVisible();
    expect(screen.getByText('★★★★★')).toBeVisible();
    expect(screen.getByText(/小红书/)).toBeVisible();
    expect(screen.getByText(/粉丝 12万/)).toBeVisible();
    expect(screen.getByText('82')).toBeVisible();
    expect(screen.getByText('重点推荐')).toBeVisible();
    expect(screen.getByText(/互动率 5\.2%/)).toBeVisible();
    expect(screen.getByText(/预估报价 ¥12,000/)).toBeVisible();
  });

  it('omits optional metric chips when the item has no export fields', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({
      total: 1,
      items: [kolSelectionItem({ nickname: '达人小B', fields: {}, score: {} })],
    });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));

    expect(await screen.findByText('达人小B')).toBeVisible();
    expect(screen.queryByText(/互动率/)).not.toBeInTheDocument();
    expect(screen.queryByText(/预估报价/)).not.toBeInTheDocument();
    expect(screen.queryByText(/综合评分/)).not.toBeInTheDocument();
  });

  it('falls back to top-level engagement/price fields when export_fields is absent', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({
      total: 1,
      items: [kolSelectionItem({ fields: { engagement_rate: 3.8, quoted_price_cny: 8000 } })],
    });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));

    expect(await screen.findByText(/互动率 3\.8%/)).toBeVisible();
    expect(screen.getByText(/预估报价 ¥8,000/)).toBeVisible();
  });

  it('shows the empty hint when the selection list is empty', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 0, items: [] });
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (0)' }));

    expect(await screen.findByText('暂无圈选达人，发起会话后自动圈选')).toBeVisible();
  });

  it('shows an inline error when the selection fetch fails', async () => {
    vi.mocked(getKolSelection).mockRejectedValue(new Error('HTTP_500'));
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('达人名单加载失败，请稍后重试');
  });

  it('does not fetch the selection list without a session id', async () => {
    render(<UniversalReport selectionCount={2} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (2)' }));

    expect(getKolSelection).not.toHaveBeenCalled();
    expect(await screen.findByText('暂无圈选达人，发起会话后自动圈选')).toBeVisible();
  });

  it('switches back to the report tab content', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: 'KOL 分析' }));
    expect(screen.getByText(/已圈选 1 位达人/)).toBeVisible();
  });
});
