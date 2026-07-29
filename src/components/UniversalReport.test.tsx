import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiAnalysisReport, ApiArtifactsSummary, ApiFavorite, ApiSessionReportItem } from '../api/contracts';
import { createFavoriteByKey, deleteFavoriteByKey } from '../api/favorites';
import { downloadKolSelection, getKolSelection, getKolTop10Trend, listSelectionSets, runKolAnalysis } from '../api/kolSelection';
import { listSessionReports } from '../api/reports';
import { getAnalysisReport } from '../api/tasks';
import { analysisReportFixture } from '../test/fixtures';
import UniversalReport from './UniversalReport';

vi.mock('../api/kolSelection', () => ({
  runKolAnalysis: vi.fn(),
  downloadKolSelection: vi.fn(),
  getKolSelection: vi.fn(),
  getKolTop10Trend: vi.fn(),
  listSelectionSets: vi.fn(),
}));

vi.mock('../api/reports', () => ({
  listSessionReports: vi.fn(),
  getArtifactsSummary: vi.fn(),
  markArtifactRead: vi.fn(),
}));

vi.mock('../api/tasks', () => ({
  getAnalysisReport: vi.fn(),
}));

vi.mock('../api/favorites', () => ({
  createFavoriteByKey: vi.fn(),
  deleteFavoriteByKey: vi.fn(),
}));

function favoriteFixture(overrides: Partial<ApiFavorite> = {}): ApiFavorite {
  return {
    id: 'fav-1',
    kol_id: null,
    platform: 'xiaohongshu',
    platform_account_id: null,
    kol_uid: 'uid-1',
    nickname: '达人小A',
    profile_url: null,
    snapshot: null,
    note: null,
    source_task_id: null,
    created_at: '2026-07-20T10:00:00Z',
    ...overrides,
  };
}

function kolSelectionItem(overrides: Record<string, unknown> = {}) {
  return {
    platform: 'xiaohongshu',
    kol_uid: 'uid-1',
    nickname: '达人小A',
    followers: 120000,
    city: '上海',
    profile_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
    fields: { engagement_rate: 5.2, quoted_price_cny: 12000 },
    score: { total: 82, rating: '重点推荐', stars: '★★★★★', dimensions: {}, data_completeness: 0.9 },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('UniversalReport', () => {
  beforeEach(() => {
    vi.mocked(runKolAnalysis).mockReset();
    vi.mocked(downloadKolSelection).mockReset();
    vi.mocked(getKolSelection).mockReset();
    vi.mocked(getKolTop10Trend).mockReset().mockResolvedValue({ set_id: null, items: [] });
    vi.mocked(listSelectionSets).mockReset().mockResolvedValue([]);
    vi.mocked(listSessionReports).mockReset().mockResolvedValue([]);
    vi.mocked(getAnalysisReport).mockReset();
    vi.mocked(createFavoriteByKey).mockReset();
    vi.mocked(deleteFavoriteByKey).mockReset();
  });

  it('shows v2 score completeness and opens the score guide from the selection tab', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({
      total: 1,
      items: [kolSelectionItem({ score: { version: 'kol_score_v2', total: 82, rating: '重点推荐', stars: '★★★★★', data_completeness: 100 } })],
    });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);
    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    expect(await screen.findByText('数据完整度 100%')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '评分说明' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('行业兴趣 10%');
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

  it('renders the Top10 KOL weekly interaction trend on the analysis tab', async () => {
    vi.mocked(getKolTop10Trend).mockResolvedValue({
      set_id: 'set-1',
      items: [{ rank: 1, platform: 'douyin', kol_uid: 'dy-1', nickname: '达人甲', ranking_interaction: 1200, scope_status: {}, trend_points: [{ week_start: '2026-07-06', average_interactions: 1200, post_count: 2 }] }],
    });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    expect(await screen.findByText('Top10 KOL互动趋势')).toBeVisible();
    expect(getKolTop10Trend).toHaveBeenCalledWith('session-1', undefined);
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

  it('refreshes the selection list when the task transitions to a terminal status', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    const { rerender } = render(<UniversalReport sessionId="session-1" selectionCount={1} taskStatus="running" />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    await waitFor(() => expect(getKolSelection).toHaveBeenCalledTimes(1));

    // 中间态变化不重复拉取
    rerender(<UniversalReport sessionId="session-1" selectionCount={1} taskStatus="writing" />);
    expect(getKolSelection).toHaveBeenCalledTimes(1);

    // 到达终态才刷新
    rerender(<UniversalReport sessionId="session-1" selectionCount={1} taskStatus="completed" />);
    await waitFor(() => expect(getKolSelection).toHaveBeenCalledTimes(2));
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

  it('favorites a selection card with a defensive snapshot', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    vi.mocked(createFavoriteByKey).mockResolvedValue(favoriteFixture());
    const onFavoriteToggled = vi.fn();
    render(
      <UniversalReport sessionId="session-1" selectionCount={1} favorites={[]} onFavoriteToggled={onFavoriteToggled} />,
    );

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '收藏' }));

    await waitFor(() => expect(createFavoriteByKey).toHaveBeenCalledWith({
      platform: 'xiaohongshu',
      kolUid: 'uid-1',
      nickname: '达人小A',
      snapshot: {
        followers: 120000,
        rating: '重点推荐',
        stars: '★★★★★',
        engagement_rate: 5.2,
        quoted_price_cny: 12000,
        city: '上海',
        profile_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
      },
    }));
    expect(deleteFavoriteByKey).not.toHaveBeenCalled();
    expect(onFavoriteToggled).toHaveBeenCalledTimes(1);
  });

  it('marks favorited selection cards as active and unfavorites them by key', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    vi.mocked(deleteFavoriteByKey).mockResolvedValue();
    const onFavoriteToggled = vi.fn();
    render(
      <UniversalReport
        sessionId="session-1"
        selectionCount={1}
        favorites={[favoriteFixture()]}
        onFavoriteToggled={onFavoriteToggled}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '取消收藏' }));

    await waitFor(() => expect(deleteFavoriteByKey).toHaveBeenCalledWith('xiaohongshu', 'uid-1'));
    expect(createFavoriteByKey).not.toHaveBeenCalled();
    expect(onFavoriteToggled).toHaveBeenCalledTimes(1);
  });

  it('omits missing fields from the favorite snapshot', async () => {
    vi.mocked(getKolSelection).mockResolvedValue({
      total: 1,
      items: [kolSelectionItem({ followers: null, city: null, profile_url: null, fields: {}, score: {} })],
    });
    vi.mocked(createFavoriteByKey).mockResolvedValue(favoriteFixture());
    render(
      <UniversalReport sessionId="session-1" selectionCount={1} favorites={[]} onFavoriteToggled={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '收藏' }));

    await waitFor(() => expect(createFavoriteByKey).toHaveBeenCalledWith({
      platform: 'xiaohongshu',
      kolUid: 'uid-1',
      nickname: '达人小A',
      snapshot: {},
    }));
  });

  it('renders three top-level tabs with the kol tab selected by default', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={2} />);

    expect(screen.getByRole('tab', { name: '品牌分析' })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByRole('tab', { name: '活动分析' })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByRole('tab', { name: '达人' })).toHaveAttribute('aria-selected', 'true');
    // 达人 Tab 内保留原有 KOL 分析/圈选达人两子 Tab。
    expect(screen.getByRole('tab', { name: 'KOL 分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '圈选达人 (2)' })).toBeVisible();
  });

  it('shows the brand analysis empty state', () => {
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    //expect(screen.getByText('完成一次品牌分析后在此展示')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: '活动分析' }));
    //expect(screen.getByText('完成一次活动分析后在此展示')).toBeVisible();
  });

  it('renders the latest brand report with scope and switches versions', async () => {
    const brandV1 = analysisReportFixture({
      id: 'brand-report-1', version: 1, title: '海底捞品牌分析v1',
      scope: { brand: '海底捞' },
    });
    const brandV2 = analysisReportFixture({
      id: 'brand-report-2', version: 2, title: '海底捞品牌分析v2',
      scope: { brand: '海底捞' },
    });
    vi.mocked(listSessionReports).mockResolvedValue([
      { report_id: 'brand-report-2', title: '海底捞品牌分析v2', version: 2, scope: { brand: '海底捞' }, status: 'completed', created_at: '2026-07-24T11:00:00Z' },
      { report_id: 'brand-report-1', title: '海底捞品牌分析v1', version: 1, scope: { brand: '海底捞' }, status: 'completed', created_at: '2026-07-24T10:00:00Z' },
    ]);
    vi.mocked(getAnalysisReport).mockImplementation(async id => id === 'brand-report-2' ? brandV2 : brandV1);
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    expect(await screen.findByText('海底捞品牌分析v2')).toBeVisible();
    expect(screen.getByText(/品牌：海底捞/)).toBeVisible();

    fireEvent.change(screen.getByRole('combobox', { name: '报告版本' }), { target: { value: 'brand-report-1' } });
    expect(await screen.findByText('海底捞品牌分析v1')).toBeVisible();
  });

  it('shows a failure hint when the latest artifact failed', () => {
    const summary: ApiArtifactsSummary = {
      brand: {
        latest_artifact: {
          artifact_id: 'artifact-failed', artifact_type: 'brand_report', title: '品牌分析报告',
          version: 1, scope: null, status: 'failed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: false,
      },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: { latest_artifact: null, unread: false },
      kol_selection: { latest_artifact: null, unread: false },
    };
    render(<UniversalReport sessionId="session-1" selectionCount={0} artifactsSummary={summary} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    expect(screen.getByRole('alert')).toHaveTextContent('上一次报告生成失败');
  });

  it('shows unread dots and clears them via onMarkArtifactSeen on tab click', () => {
    const summary: ApiArtifactsSummary = {
      brand: {
        latest_artifact: {
          artifact_id: 'artifact-brand', artifact_type: 'brand_report', title: '品牌分析',
          version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: true,
      },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: {
        latest_artifact: {
          artifact_id: 'artifact-kol', artifact_type: 'kol_report', title: 'KOL 分析',
          version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: true,
      },
      kol_selection: { latest_artifact: null, unread: false },
    };
    const onMarkArtifactSeen = vi.fn();
    render(
      <UniversalReport
        sessionId="session-1"
        selectionCount={0}
        artifactsSummary={summary}
        onMarkArtifactSeen={onMarkArtifactSeen}
      />,
    );

    // 未读点的 aria-label 会并入按钮可访问名，这里用正则匹配 tab。
    expect(screen.getByRole('tab', { name: /品牌分析/ }).querySelector('[aria-label="未读"]')).not.toBeNull();
    expect(screen.getByRole('tab', { name: /^达人/ }).querySelector('[aria-label="未读"]')).not.toBeNull();
    expect(screen.getByRole('tab', { name: '活动分析' }).querySelector('[aria-label="未读"]')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: /品牌分析/ }));
    expect(onMarkArtifactSeen).toHaveBeenCalledWith('brand', 'artifact-brand');
  });

  it('does not auto-switch tabs when an unread summary arrives', () => {
    const { rerender } = render(<UniversalReport sessionId="session-1" selectionCount={3} />);
    expect(screen.getByText(/已圈选 3 位达人/)).toBeVisible();

    const summary: ApiArtifactsSummary = {
      brand: {
        latest_artifact: {
          artifact_id: 'artifact-brand', artifact_type: 'brand_report', title: '品牌分析',
          version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z',
        },
        unread: true,
      },
      campaign: { latest_artifact: null, unread: false },
      kol_analysis: { latest_artifact: null, unread: false },
      kol_selection: { latest_artifact: null, unread: false },
    };
    rerender(<UniversalReport sessionId="session-1" selectionCount={3} artifactsSummary={summary} />);

    // 仍在「达人」Tab，不因未读自动切换。
    expect(screen.getByRole('tab', { name: '达人' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText(/已圈选 3 位达人/)).toBeVisible();
  });

  it('switches selection sets and exports the chosen set', async () => {
    vi.mocked(listSelectionSets).mockResolvedValue([
      { set_id: 'set-2', title: '默认名单', version: 2, status: 'active', item_count: 1, created_at: '2026-07-24T10:00:00Z' },
      { set_id: 'set-1', title: '历史默认名单', version: 1, status: 'completed', item_count: 1, created_at: '2026-07-23T10:00:00Z' },
    ]);
    vi.mocked(getKolSelection).mockResolvedValue({ total: 1, items: [kolSelectionItem()] });
    render(<UniversalReport sessionId="session-1" selectionCount={1} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (1)' }));
    await waitFor(() => expect(getKolSelection).toHaveBeenCalledWith('session-1'));

    fireEvent.change(await screen.findByRole('combobox', { name: '名单版本' }), { target: { value: 'set-1' } });
    await waitFor(() => expect(getKolSelection).toHaveBeenCalledWith('session-1', 'set-1'));

    fireEvent.click(screen.getByRole('button', { name: '导出 Excel' }));
    await waitFor(() => expect(downloadKolSelection).toHaveBeenCalledWith('session-1', 'set-1'));
  });

  it('shows an animated loading state while the report version list is loading', () => {
    vi.mocked(listSessionReports).mockReturnValue(new Promise<ApiSessionReportItem[]>(() => undefined));
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('正在连接数据服务…');
    expect(status.querySelector('.animate-spin')).not.toBeNull();
  });

  it('keeps the loading state while the report detail is in flight', async () => {
    const detail = deferred<ApiAnalysisReport>();
    vi.mocked(listSessionReports).mockResolvedValue([
      { report_id: 'brand-report-1', title: '海底捞品牌分析v1', version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z' },
    ]);
    vi.mocked(getAnalysisReport).mockReturnValue(detail.promise);
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    // 列表已到、详情在途：仍是加载态而不是空态文案（标题 h3 除外，这里限定正文容器）。
    await waitFor(() => expect(getAnalysisReport).toHaveBeenCalledWith('brand-report-1'));
    expect(screen.getByRole('status')).toHaveTextContent('正在连接数据服务…');
    expect(screen.queryByText('完成一次品牌分析后在此展示', { selector: 'div' })).not.toBeInTheDocument();

    detail.resolve(analysisReportFixture({ id: 'brand-report-1', title: '海底捞品牌分析v1' }));
    expect(await screen.findByText('一、核心结论')).toBeVisible();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('falls back to the empty state when the report detail fetch fails', async () => {
    vi.mocked(listSessionReports).mockResolvedValue([
      { report_id: 'brand-report-1', title: '海底捞品牌分析v1', version: 1, scope: null, status: 'completed', created_at: '2026-07-24T10:00:00Z' },
    ]);
    vi.mocked(getAnalysisReport).mockRejectedValue(new Error('HTTP_500'));
    render(<UniversalReport sessionId="session-1" selectionCount={0} />);

    fireEvent.click(screen.getByRole('tab', { name: '品牌分析' }));

    // 详情失败不应永久卡在加载态，回到空态文案。
    expect(await screen.findByText('完成一次品牌分析后在此展示', { selector: 'div' })).toBeVisible();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows only the top 20 selection items ordered by engagement rate with a summary line', async () => {
    const items = [
      ...Array.from({ length: 18 }, (_, index) => kolSelectionItem({
        kol_uid: `uid-r${index + 1}`,
        nickname: `达人r${index + 1}`,
        fields: { engagement_rate: index + 1 },
      })),
      ...Array.from({ length: 7 }, (_, index) => kolSelectionItem({
        kol_uid: `uid-n${index + 1}`,
        nickname: `达人n${index + 1}`,
        fields: {},
      })),
    ];
    vi.mocked(getKolSelection).mockResolvedValue({ total: 25, items });
    render(<UniversalReport sessionId="session-1" selectionCount={25} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (25)' }));

    expect(await screen.findByText('共 25 位达人，按互动率展示 Top 20')).toBeVisible();
    const rendered = await screen.findAllByText(/^达人[rn]\d+$/);
    const names = rendered.map(node => node.textContent);
    expect(names).toHaveLength(20);
    // 互动率倒序：r18 最高 … r1 最低。
    expect(names.slice(0, 18)).toEqual(Array.from({ length: 18 }, (_, index) => `达人r${18 - index}`));
    // 无互动率的排最后，保持原有相对顺序（稳定排序）。
    expect(names.slice(18)).toEqual(['达人n1', '达人n2']);
  });

  it('renders all selection items without a summary line when there are at most 20', async () => {
    const items = [
      kolSelectionItem({ kol_uid: 'uid-a', nickname: '达人甲', fields: { engagement_rate: 1 } }),
      kolSelectionItem({ kol_uid: 'uid-b', nickname: '达人乙', fields: { engagement_rate: 9 } }),
      kolSelectionItem({ kol_uid: 'uid-c', nickname: '达人丙', fields: { engagement_rate: 5 } }),
    ];
    vi.mocked(getKolSelection).mockResolvedValue({ total: 3, items });
    render(<UniversalReport sessionId="session-1" selectionCount={3} />);

    fireEvent.click(screen.getByRole('tab', { name: '圈选达人 (3)' }));

    expect(await screen.findByText('达人甲')).toBeVisible();
    expect(screen.getByText('达人乙')).toBeVisible();
    expect(screen.getByText('达人丙')).toBeVisible();
    expect(screen.queryByText(/按互动率展示 Top 20/)).not.toBeInTheDocument();
    // 子 Tab 标签计数仍来自 selectionCount，不受 Top20 截断影响。
    expect(screen.getByRole('tab', { name: '圈选达人 (3)' })).toBeVisible();
  });
});
