import { expect, test } from '@playwright/test';


test('agent task session restores and renders the universal analysis report', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8);
  const session = {
    id: 'session-agent', title: '美妆行业分析', brand: '', campaign_name: null,
    status: 'completed', platforms: [], category: '美妆', target_audience: '',
    budget_min: null, budget_max: null, filters: {}, is_starred: false, messages: [],
    latest_task: { id: 'task-agent', status: 'completed', kind: 'agent', completed_at: '2026-07-15T10:00:00Z' },
    latest_candidates: null,
    latest_report: null,
    latest_analysis_report: {
      id: 'report-agent', task_id: 'task-agent', version: 1,
      title: '美妆行业社交媒体分析报告', status: 'completed', generated_at: '2026-07-15T10:00:00Z',
    },
    created_at: '2026-07-15T10:00:00Z', updated_at: '2026-07-15T10:00:00Z',
  };
  const report = {
    id: 'report-agent', task_id: 'task-agent', version: 1,
    title: '美妆行业社交媒体分析报告', conclusion: '行业整体仍在增长通道。',
    status: 'completed', generated_at: '2026-07-15T10:00:00Z',
    blocks: [
      { type: 'heading', text: '整体热度' },
      { type: 'metric_grid', title: '三平台合计', items: [
        { label: '总声量', value: 4373, unit: '万帖', delta: '+37.2%' },
        { label: '总互动数', value: 148300, unit: '万', delta: '+57.6%' },
      ] },
      { type: 'line_chart', title: '月度声量趋势', categories: ['2026-01', '2026-02', '2026-03'], series: [
        { name: '小红书', values: [33.7, 24.9, 62.2] },
        { name: '抖音', values: [97.6, 69.6, 93.0] },
      ] },
      { type: 'pie_chart', title: '情感占比', categories: ['正面', '中性', '负面'], series: [
        { name: '情感', values: [15.5, 83.4, 1.1] },
      ] },
      { type: 'table', title: '子品类 TOP3', columns: ['子品类', '声量占比', '声量同比'], rows: [
        ['面部护理', '52.0%', '+0.9%'],
        ['面部彩妆', '18.7%', '+8.5%'],
        ['香水', '11.2%', '-0.7%'],
      ] },
      { type: 'tag_list', title: '热门话题', items: ['#美妆分享#', '#平价彩妆#', '#新手化妆教程#'] },
      { type: 'markdown', text: '热度同比大幅扩张但环比回落，行业仍在增长通道。' },
      { type: 'sources', items: [{ name: 'DataTap 社媒统计', collected_at: '2026-07-15T10:00:00Z', evidence: 'evidence-agent' }] },
    ],
  };

  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(`138${suffix}`);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();

  await page.route('**/api/v1/sessions/session-agent', route => route.fulfill({ json: session }));
  await page.route('**/api/v1/sessions', route => route.fulfill({ json: [session] }));
  await page.route('**/api/v1/analysis-reports/report-agent', route => route.fulfill({ json: report }));

  await page.reload();
  const mobileNavigation = page.getByRole('navigation', { name: '移动工作区导航' });
  if (await mobileNavigation.isVisible()) {
    await mobileNavigation.getByRole('button', { name: '分析报告' }).click();
  }
  await expect(page.getByText('美妆行业社交媒体分析报告', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('整体热度', { exact: true })).toBeVisible();
  await expect(page.getByText('总声量', { exact: true })).toBeVisible();
  await expect(page.getByText('月度声量趋势', { exact: true })).toBeVisible();
  await expect(page.getByText('面部彩妆', { exact: true })).toBeVisible();
  await expect(page.getByText('#平价彩妆#', { exact: true })).toBeVisible();
  await expect(page.getByText('DataTap 社媒统计', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'output/playwright/analysis-report.png', fullPage: true });
});
