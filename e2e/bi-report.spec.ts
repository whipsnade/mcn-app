import { expect, test } from '@playwright/test';


test('restored matching candidate and report versions render the KOL decision BI', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8);
  const session = {
    id: 'session-bi', title: '示例品牌-KOL 决策', brand: '示例品牌', campaign_name: 'KOL 决策',
    status: 'completed', platforms: ['bilibili'], category: '美妆', target_audience: '通勤女性',
    budget_min: null, budget_max: null, filters: {}, is_starred: false, messages: [],
    latest_task: { id: 'task-bi', status: 'completed', completed_at: '2026-07-15T10:00:00Z' },
    latest_candidates: { task_id: 'task-bi', version: 2, total: 1 },
    latest_report: { id: 'report-bi', task_id: 'task-bi', report_version: 1, candidate_version: 2, status: 'completed', generated_at: '2026-07-15T10:00:00Z' },
    created_at: '2026-07-15T10:00:00Z', updated_at: '2026-07-15T10:00:00Z',
  };

  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(`139${suffix}`);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();

  await page.route('**/api/v1/sessions/session-bi', route => route.fulfill({ json: session }));
  await page.route('**/api/v1/sessions', route => route.fulfill({ json: [session] }));
  await page.route('**/api/v1/tasks/task-bi/candidates', route => route.fulfill({ json: {
    task_id: 'task-bi', version: 2, total: 1, items: [{
      id: 'candidate-bi', kol_id: 'kol-bi', platform: 'bilibili', platform_account_id: '报告达人', nickname: '报告达人',
      profile_url: null, rank: 1, total_score: 91, scores: { audience: 82 }, matched_conditions: [], risks: [], recommendation: '优先联系',
    }],
  } }));
  await page.route('**/api/v1/reports/report-bi', route => route.fulfill({ json: {
    id: 'report-bi', task_id: 'task-bi', report_version: 1, candidate_version: 2,
    overview: { candidate_count: 1, top_score: 91 }, score_composition: [{ dimension: 'audience', average: 82 }],
    audience_content_fit: { audience: 82 }, platform_distribution: [{ platform: 'bilibili', count: 1 }],
    budget_analysis: { average_budget_score: 80 }, comparison: [{ platform_account_id: '报告达人', total_score: 91 }], risks: [],
    conclusion: '优先联系报告达人。', sources: [{ tool_name_cn: 'B站数据采集', collected_at: '2026-07-15T10:00:00Z', evidence_id: 'evidence-bi' }], generated_at: '2026-07-15T10:00:00Z',
  } }));

  await page.reload();
  const mobileNavigation = page.getByRole('navigation', { name: '移动工作区导航' });
  if (await mobileNavigation.isVisible()) {
    await mobileNavigation.getByRole('button', { name: 'BI 报告' }).click();
  }
  for (const title of ['任务概览', '评分构成', '受众与内容匹配', '平台分布', '预算与性价比', '候选对比', '风险与数据质量', 'AI 结论', '数据来源']) {
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  }
  const comparison = page.locator('section').filter({ has: page.getByRole('heading', { name: '候选对比' }) });
  await expect(comparison.getByText(/报告达人/)).toBeVisible();
  await expect(page.getByText('B站数据采集', { exact: true })).toBeVisible();
});
