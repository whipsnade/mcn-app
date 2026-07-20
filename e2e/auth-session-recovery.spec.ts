import { expect, test } from '@playwright/test';


interface MockSessionMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sequence: number;
  metadata: Record<string, unknown>;
  created_at: string;
}

test('blank session clarifies through brainstorm and restores after reload', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8);
  const phone = `139${suffix}`;
  const firstMessage = `想分析新品防晒-${suffix}`;
  const firstQuestion = `想分析哪个平台？${suffix}`;
  const secondQuestion = `分析目标是什么？${suffix}`;

  const messages: MockSessionMessage[] = [];
  let created = false;
  const session = {
    id: 'session-blank',
    title: '新会话1',
    brand: '',
    campaign_name: null,
    status: 'draft',
    platforms: [],
    category: '',
    target_audience: '',
    budget_min: null,
    budget_max: null,
    filters: {},
    is_starred: false,
    messages,
    latest_task: null,
    latest_analysis_report: null,
    created_at: '2026-07-20T10:00:00Z',
    updated_at: '2026-07-20T10:00:00Z',
  };
  const profile = {
    brand: null, category: null, platforms: [], audience: null,
    period: null, kol_filters: null, goal: null,
  };
  const pushMessage = (role: 'user' | 'assistant', content: string, metadata: Record<string, unknown> = {}) => {
    const message: MockSessionMessage = {
      id: `m-${messages.length + 1}`,
      role,
      content,
      sequence: messages.length + 1,
      metadata,
      created_at: new Date().toISOString(),
    };
    messages.push(message);
    return message;
  };

  await page.route('**/api/v1/sessions', route => {
    if (route.request().method() === 'POST') {
      created = true;
      return route.fulfill({ json: session });
    }
    return route.fulfill({ json: created ? [session] : [] });
  });
  await page.route('**/api/v1/sessions/session-blank', route => route.fulfill({ json: session }));

  const rounds = [
    { question: firstQuestion, options: ['小红书', '抖音'] },
    { question: secondQuestion, options: ['声量口碑', '达人投放'] },
  ];
  let brainstormCalls = 0;
  await page.route('**/api/v1/sessions/session-blank/brainstorm', route => {
    const body = route.request().postDataJSON() as { content: string };
    const round = rounds[Math.min(brainstormCalls, rounds.length - 1)];
    brainstormCalls += 1;
    pushMessage('user', body.content);
    const assistant = pushMessage('assistant', round.question, {
      brainstorm: { ready: false, options: round.options, profile_summary: profile },
    });
    return route.fulfill({
      json: { ready: false, task_id: null, message: assistant, profile },
    });
  });

  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(phone);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();

  await expect(page.getByText('1,000 / 5,000 点')).toBeVisible();
  await expect(page.getByTitle('管理员控制台')).toHaveCount(0);

  // 空白会话：点新建直接进入对话窗口，无表单。
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '新会话1' })).toBeVisible();
  await expect(page.getByText(/渠道:/)).toHaveCount(0);

  // 画像未 ready：发消息走 brainstorm 澄清，一次一问 + 选项 chips。
  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill(firstMessage);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByText(firstQuestion, { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '小红书', exact: true })).toBeVisible();
  await expect(page.getByText(firstMessage, { exact: true })).toBeVisible();

  // 点选项即以该文本继续澄清。
  await page.getByRole('button', { name: '小红书', exact: true }).click();
  await expect(page.getByText(secondQuestion, { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '达人投放', exact: true })).toBeVisible();

  await page.reload();

  const mobileNavigation = page.getByRole('navigation', { name: '移动工作区导航' });
  if (await mobileNavigation.isVisible()) {
    await expect(page.getByText('1,000 / 5,000 点')).toBeVisible();
    await page.getByRole('button', { name: '分析对话' }).click();
  }
  await expect(page.getByText(firstMessage, { exact: true })).toBeVisible();
  await expect(page.getByText(firstQuestion, { exact: true })).toBeVisible();
  await expect(page.getByText(secondQuestion, { exact: true })).toBeVisible();
  if (!await mobileNavigation.isVisible()) {
    await expect(page.getByText('1,000 / 5,000 点')).toBeVisible();
  }
});
