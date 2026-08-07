import { expect, test, type Page } from '@playwright/test';

// --------------------------------------------------------------------------- //
// 统一 Agent 运行时 E2E（design §13.1 / Task 25）。
//
// 全部会话/Run/Artifact 数据经 page.route 注入 NEW Agent API fixture（真实
// sequence / run_id / artifact_id / parent_artifact_id，事件类型
// run.* / thinking.* / tool.* / artifact.draft.* / review.* /
// artifact.published / message.completed）。不触碰任何旧 task.* 事件或旧
// /sessions/{id}/tasks、/analysis-reports、旧 brand export 路由。
//
// 登录本身走真实后端 AUTH_MODE=mock（短信验证码自动回填），其余请求全部路由。
// --------------------------------------------------------------------------- //

const AUTH_PHONE_PREFIX = '137';
const BASE_TIMESTAMP = '2026-08-02T10:00:00';

interface SseEvent {
  seq: number;
  event: string;
  payload?: Record<string, unknown>;
}

function sessionJson(id: string, title: string): Record<string, unknown> {
  return {
    id,
    title,
    status: 'active',
    created_at: '2026-08-01T10:00:00',
    updated_at: BASE_TIMESTAMP,
  };
}

function messageJson(
  id: string,
  role: string,
  content: string,
  sequence: number,
  runId: string | null,
  metadata?: Record<string, unknown> | null,
): Record<string, unknown> {
  return { id, role, content, sequence, run_id: runId, created_at: BASE_TIMESTAMP, ...(metadata ? { metadata } : {}) };
}

function runJson(id: string, sessionId: string, status: string): Record<string, unknown> {
  return {
    id,
    session_id: sessionId,
    parent_run_id: null,
    profile_name: 'session_analyst_v1',
    status,
    outcome: null,
    decision_count: 1,
    review_count: 0,
    revision_count: 0,
    error_code: null,
    started_at: BASE_TIMESTAMP,
    paused_at: null,
    completed_at: null,
  };
}

/** SSE 编码（对齐 backend app/agent_runtime/sse.encode_sse_event）。 */
function sseBody(runId: string, events: SseEvent[]): string {
  return events.map(({ seq, event, payload = {} }) => (
    `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify({ ...payload, run_id: runId })}\n\n`
  )).join('');
}

// --------------------------------------------------------------------------- //
// 共享辅助
// --------------------------------------------------------------------------- //

async function login(page: Page, phone: string) {
  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(phone);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();
}

async function uniquePhone(): Promise<string> {
  return `${AUTH_PHONE_PREFIX}${Date.now().toString().slice(-8)}`;
}

async function mockWalletAndFavorites(page: Page) {
  await page.route('**/api/v1/wallet', route => route.fulfill({ json: { balance: 100, reserved: 0, available: 100 } }));
  await page.route('**/api/v1/favorites', route => route.fulfill({ json: [] }));
}

async function mockArtifactsEmpty(page: Page, sessionId: string) {
  await page.route(`**/api/v1/agent/sessions/${sessionId}/artifacts`, route => route.fulfill({ json: [] }));
}

/** 小视口（<1280px）先切到分析对话面板（桌面 xl 常显，无需切换）。 */
async function ensureChatPane(page: Page) {
  if ((page.viewportSize()?.width ?? 1440) >= 1280) return;
  await page.getByRole('navigation', { name: '移动工作区导航' }).getByRole('button', { name: '分析对话' }).click();
}

// --------------------------------------------------------------------------- //
// 1. 独立 Run 卡：thinking / 工具步骤 / 直接发布 / 部分成功 / 终态折叠
// --------------------------------------------------------------------------- //

test('keeps two direct-publish runs independent and exposes partial publication failures', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-new';
  const newSession = sessionJson(sessionId, '新会话1');
  const run1 = 'run-1';
  const run2 = 'run-2';
  const question = '分析一下海底捞的品牌声量';

  // 会话详情随「已发送消息」推进：settle 回拉后消息带 run_id，Run 卡锚定保留。
  let sentMessages: Array<Record<string, unknown>> = [];
  const detail = () => ({
    ...newSession,
    messages: sentMessages,
    runs: sentMessages.length
      ? [runJson(run1, sessionId, 'completed'), runJson(run2, sessionId, 'completed')]
      : [],
  });

  // run-1 直接发布：品牌报告成功，活动报告校验失败，Run 以部分完成收尾。
  // fixture 中刻意没有 review.*，防止旧 Reviewer 流程被悄然接回。
  const run1Events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'thinking.started', payload: { attempt: 1 } },
    { seq: 3, event: 'thinking.delta', payload: { text: '正在检索品牌声量…' } },
    { seq: 4, event: 'thinking.completed' },
    { seq: 5, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
    { seq: 6, event: 'tool.succeeded', payload: { internal_tool_name: 'brand_search', duration_ms: 1200, points: 10 } },
    { seq: 7, event: 'artifact.draft.created', payload: { artifact_id: 'art-brand', draft_id: 'draft-brand', module: 'brand', version: 1, status: 'draft', title: '品牌报告 v1' } },
    { seq: 8, event: 'artifact.draft.created', payload: { artifact_id: 'art-campaign', draft_id: 'draft-campaign', module: 'campaign', version: 1, status: 'draft', title: '活动报告 v1' } },
    {
      seq: 9,
      event: 'artifact.publish.completed',
      payload: {
        published: 1,
        validation_failed: 1,
        failed: 0,
        items: [
          { artifact_id: 'art-brand', draft_id: 'draft-brand', version: 1, status: 'published' },
          { artifact_id: 'art-campaign', draft_id: 'draft-campaign', version: 1, status: 'validation_failed' },
        ],
      },
    },
    { seq: 10, event: 'message.completed', payload: { type: 'completion', content: '品牌完成，活动报告待修复' } },
    { seq: 11, event: 'run.completed_with_warnings', payload: { outcome: 'completed_with_warnings' } },
  ];
  // run-2：独立终态序列，不得吸收 run-1 的事件。
  const run2Events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'douyin_search' } },
    { seq: 3, event: 'tool.succeeded', payload: { internal_tool_name: 'douyin_search', duration_ms: 800, points: 10 } },
    { seq: 4, event: 'message.completed', payload: { type: 'completion', content: '已完成抖音渠道分析' } },
    { seq: 5, event: 'run.completed', payload: { outcome: 'completed' } },
  ];

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: newSession });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await mockArtifactsEmpty(page, sessionId);
  let messageCount = 0;
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    const body = route.request().postDataJSON() as { content: string };
    messageCount += 1;
    const runId = messageCount === 1 ? run1 : run2;
    const seq = messageCount;
    sentMessages = [
      messageJson('m-user-1', 'user', question, 1, run1),
      messageJson('m-user-2', 'user', '再分析抖音渠道', 2, run2),
    ].slice(0, messageCount);
    return route.fulfill({
      status: 201,
      json: { run_id: runId, session_id: sessionId, message_id: `m-user-${seq}`, status: 'queued', reused: false },
    });
  });
  await page.route(`**/api/v1/agent/runs/${run1}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(run1, run1Events),
  }));
  await page.route(`**/api/v1/agent/runs/${run2}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(run2, run2Events),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '新会话1' })).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill(question);
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // Run 直接进入终态摘要；不出现任何 Reviewer 状态。
  const collapsed = page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 部分完成/ });
  await expect(collapsed).toBeVisible();
  await collapsed.click();

  // 展开后可回看 thinking / 工具 / 每项发布结果；思考默认收起。
  const expanded = page.getByRole('region', { name: '执行卡' }).first();
  const thinking = expanded.getByRole('button', { name: '已思考' });
  await expect(thinking).toHaveAttribute('aria-expanded', 'false');
  await thinking.click();
  await expect(expanded.getByText('正在检索品牌声量…', { exact: true })).toBeVisible();

  await expect(expanded.getByText('brand_search', { exact: true })).toBeVisible();
  await expect(expanded.getByText('成功', { exact: true })).toBeVisible();
  await expect(expanded.getByText('1.2 秒', { exact: true })).toBeVisible();
  await expect(expanded.getByText('10 积分', { exact: true })).toBeVisible();
  await expect(expanded.getByText('品牌报告已发布', { exact: true })).toBeVisible();
  await expect(expanded.getByText('活动报告发布校验失败', { exact: true })).toBeVisible();
  await expect(expanded.getByText(/审核|复核/, { exact: false })).toHaveCount(0);

  // 收起第一张卡后，再次发送产生第二条消息与第二张卡（完成 Run 不吸收下一轮事件）。
  await page.getByRole('button', { name: '收起' }).first().click();
  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('再分析抖音渠道');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  // C3：run-1 转为历史后经事件回放补齐步骤，两张卡都带真实步数（不再是空壳卡）。
  const collapsedCards = page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · (部分完成|分析完成)/ });
  await expect(collapsedCards).toHaveCount(2);

  // 展开历史卡（第一张，锚定在第一轮消息下）可回看回放出的工具步骤。
  await collapsedCards.first().click();
  const historyCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(historyCard.getByText('brand_search', { exact: true })).toBeVisible();
});

// --------------------------------------------------------------------------- //
// 2. 澄清：ask_user 收尾 Run，展示问题与「等待补充信息」
// --------------------------------------------------------------------------- //

test('asks clarification for a fuzzy request without MCP, then creates a child run from the answer', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-clarify';
  const newSession = sessionJson(sessionId, '澄清会话');
  const runId = 'run-clarify';
  const childRunId = 'run-clarify-child';
  const question = '想分析哪个平台？';
  const options = ['小红书', '抖音'];

  let sentMessages: Array<Record<string, unknown>> = [];
  const detail = () => ({
    ...newSession,
    messages: sentMessages,
    runs: sentMessages.length === 0
      ? []
      : sentMessages.length <= 2
        ? [runJson(runId, sessionId, 'clarification_requested')]
        : [
          runJson(runId, sessionId, 'clarification_requested'),
          { ...runJson(childRunId, sessionId, 'completed'), parent_run_id: runId },
        ],
  });

  // 澄清为 message.completed + type=clarification，SSE 不关闭（等待用户回答）。
  const events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'message.completed', payload: { type: 'clarification', question, options } },
  ];
  const childEvents: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user', parent_run_id: runId } },
    { seq: 2, event: 'run.completed', payload: { outcome: 'completed' } },
    { seq: 3, event: 'message.completed', payload: { type: 'completion', content: '已按小红书继续分析' } },
  ];
  let mcpRequestCount = 0;

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: newSession });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route('**/mcp/**', route => {
    mcpRequestCount += 1;
    return route.fulfill({ status: 500, json: { detail: 'MCP 不应在澄清阶段被调用' } });
  });
  let messageCount = 0;
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    const body = route.request().postDataJSON() as { content: string };
    messageCount += 1;
    // settle 回拉后带 assistant 澄清消息（问题文本 + metadata），供 Run 卡澄清区展示。
    sentMessages = messageCount === 1
      ? [
        messageJson('m-user-1', 'user', body.content, 1, runId),
        messageJson('m-ai-1', 'assistant', question, 2, runId, {
          type: 'clarification',
          question,
          options,
        }),
      ]
      : [
        messageJson('m-user-1', 'user', '帮我圈选达人', 1, runId),
        messageJson('m-ai-1', 'assistant', question, 2, runId, {
          type: 'clarification',
          question,
          options,
        }),
        messageJson('m-user-2', 'user', body.content, 3, childRunId),
        messageJson('m-ai-2', 'assistant', '已按小红书继续分析', 4, childRunId),
      ];
    return route.fulfill({
      status: 201,
      json: {
        run_id: messageCount === 1 ? runId : childRunId,
        session_id: sessionId,
        message_id: `m-user-${messageCount}`,
        status: 'queued',
        reused: false,
      },
    });
  });
  await page.route(`**/api/v1/agent/runs/${runId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(runId, events),
  }));
  await page.route(`**/api/v1/agent/runs/${childRunId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(childRunId, childEvents),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '澄清会话' })).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('帮我圈选达人');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // Run 卡进入「等待补充信息」并展示问题文本（状态标签与活动文案同为该文本）。
  const runCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(runCard.getByText('等待补充信息', { exact: true }).first()).toBeVisible();
  // 问题文本在消息流（澄清卡与消息气泡各一处；会话列表摘要也会带最后一句话，
  // 小视口下会命中隐藏卡片，需限定在 log 内）。
  await expect(page.getByRole('log', { name: '会话消息' }).getByText(question, { exact: true }).first()).toBeVisible();

  // 选项 chips 由 Run 卡澄清区渲染（toChatMessage 映射 clarify metadata）；点击只
  // 填入输入框，不自动提交。
  const optionChip = page.getByRole('button', { name: options[0], exact: true }).first();
  await expect(optionChip).toBeVisible();
  await optionChip.click();
  await expect(page.getByPlaceholder(/输入消息并向 AI 分析师提问/)).toHaveValue(options[0]);
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // 澄清回答只创建子 Run；本轮澄清事件不含工具调用，浏览器也不应请求 MCP。
  await expect(page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 分析完成/ })).toBeVisible();
  await expect.poll(() => mcpRequestCount).toBe(0);
});

// --------------------------------------------------------------------------- //
// 3. 证据上传：仅已解析文件进入新 Run 的 upload_ids
// --------------------------------------------------------------------------- //

test('attaches a parsed CSV evidence upload to the new analysis run', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-upload';
  const runId = 'run-upload';
  const session = sessionJson(sessionId, '资料会话');
  let sent = false;
  let messageBody: Record<string, unknown> | undefined;
  let uploadRequestCount = 0;

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: session });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({
    json: {
      ...session,
      messages: sent ? [messageJson('m-upload', 'user', '基于资料分析', 1, runId)] : [],
      runs: sent ? [runJson(runId, sessionId, 'completed')] : [],
    },
  }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route(`**/api/v1/agent/sessions/${sessionId}/uploads`, route => {
    uploadRequestCount += 1;
    expect(route.request().headers()['content-type']).toContain('multipart/form-data');
    return route.fulfill({
      status: 201,
      json: {
        id: 'upload-1',
        original_filename: 'evidence.csv',
        mime_type: 'text/csv',
        size_bytes: 20,
        sha256: 'hash',
        status: 'parsed',
        error_code: null,
        created_at: BASE_TIMESTAMP,
        completed_at: BASE_TIMESTAMP,
      },
    });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    messageBody = route.request().postDataJSON() as Record<string, unknown>;
    sent = true;
    return route.fulfill({
      status: 201,
      json: { run_id: runId, session_id: sessionId, message_id: 'm-upload', status: 'queued', reused: false },
    });
  });
  await page.route(`**/api/v1/agent/runs/${runId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(runId, [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'run.completed', payload: { outcome: 'completed' } },
    ]),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '资料会话' })).toBeVisible();
  await page.getByRole('button', { name: '上传资料' }).setInputFiles({
    name: 'evidence.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('name,value\n声量,100\n'),
  });
  await expect.poll(() => uploadRequestCount).toBe(1);
  const parsedUpload = page.getByText(/evidence\.csv · 已解析/);
  await expect(parsedUpload).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('基于资料分析');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(() => messageBody?.upload_ids).toEqual(['upload-1']);
  await expect(parsedUpload).toHaveCount(0);
});

// --------------------------------------------------------------------------- //
// 4. 无 thinking 事件：不可展开的「正在处理」
// --------------------------------------------------------------------------- //

test('without thinking events renders a non-expandable processing row', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-nothink';
  const newSession = sessionJson(sessionId, '无思考会话');
  const runId = 'run-nothink';

  let sentMessages: Array<Record<string, unknown>> = [];
  const detail = () => ({
    ...newSession,
    messages: sentMessages,
    runs: sentMessages.length ? [runJson(runId, sessionId, 'running')] : [],
  });

  const events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
  ];

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: newSession });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    const body = route.request().postDataJSON() as { content: string };
    sentMessages = [messageJson('m-user-1', 'user', body.content, 1, runId)];
    return route.fulfill({
      status: 201,
      json: { run_id: runId, session_id: sessionId, message_id: 'm-user-1', status: 'queued', reused: false },
    });
  });
  await page.route(`**/api/v1/agent/runs/${runId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(runId, events),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '无思考会话' })).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('分析品牌');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  const runCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(runCard.getByText('执行中', { exact: true })).toBeVisible();
  // 无 thinking.* 事件：只显示不可展开的「正在处理」，不出现「已思考」折叠区。
  await expect(runCard.getByText('正在处理', { exact: true })).toBeVisible();
  await expect(runCard.getByRole('button', { name: '已思考' })).toHaveCount(0);
});

// --------------------------------------------------------------------------- //
// 4. 暂停与恢复
// --------------------------------------------------------------------------- //

test('pauses an active run with the input pause button', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-pause';
  const newSession = sessionJson(sessionId, '暂停会话');
  const runId = 'run-pause';

  let sentMessages: Array<Record<string, unknown>> = [];
  const detail = () => ({
    ...newSession,
    messages: sentMessages,
    runs: sentMessages.length ? [runJson(runId, sessionId, 'cancelled')] : [],
  });

  let phase: 'running' | 'cancelled' = 'running';
  const events = (): SseEvent[] => phase === 'running'
    ? [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
    ]
    : [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
      { seq: 3, event: 'run.cancelled' },
    ];

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: newSession });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    const body = route.request().postDataJSON() as { content: string };
    sentMessages = [messageJson('m-user-1', 'user', body.content, 1, runId)];
    return route.fulfill({
      status: 201,
      json: { run_id: runId, session_id: sessionId, message_id: 'm-user-1', status: 'queued', reused: false },
    });
  });
  await page.route(`**/api/v1/agent/runs/${runId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(runId, events()),
  }));
  await page.route(`**/api/v1/agent/runs/${runId}/cancel`, route => route.fulfill({
    json: runJson(runId, sessionId, 'cancelled'),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '暂停会话' })).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('分析品牌');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  const runCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(runCard.getByText('执行中', { exact: true })).toBeVisible();

  // 输入区的暂停按钮（与 Run 卡头部暂停按钮并列，以 form 作用域区分）。
  const inputPause = page.locator('form[aria-label="发送消息"]').getByRole('button', { name: '暂停' });
  await expect(inputPause).toBeVisible();
  await inputPause.click();

  phase = 'cancelled';
  // 暂停后 Run 终态：输入恢复为「发送」按钮，Run 卡折叠为「已取消」。
  await expect(page.locator('form[aria-label="发送消息"]').getByRole('button', { name: '发送' })).toBeVisible();
  await expect(page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 已取消/ })).toBeVisible();
});

test('resumes a paused run with the continue button', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-resume';
  const newSession = sessionJson(sessionId, '恢复会话');
  const runId = 'run-resume';

  let sentMessages: Array<Record<string, unknown>> = [];
  const detail = () => ({
    ...newSession,
    messages: sentMessages,
    runs: sentMessages.length ? [runJson(runId, sessionId, 'completed')] : [],
  });

  let phase: 'paused' | 'resumed' = 'paused';
  const events = (): SseEvent[] => phase === 'paused'
    ? [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'run.paused' },
    ]
    : [
      { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
      { seq: 2, event: 'run.paused' },
      { seq: 3, event: 'run.resumed' },
      { seq: 4, event: 'run.completed', payload: { outcome: 'completed' } },
      { seq: 5, event: 'message.completed', payload: { type: 'completion', content: '已完成分析' } },
    ];

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: newSession });
    return route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail() }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages`, route => {
    const body = route.request().postDataJSON() as { content: string };
    sentMessages = [messageJson('m-user-1', 'user', body.content, 1, runId)];
    return route.fulfill({
      status: 201,
      json: { run_id: runId, session_id: sessionId, message_id: 'm-user-1', status: 'queued', reused: false },
    });
  });
  await page.route(`**/api/v1/agent/runs/${runId}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(runId, events()),
  }));
  await page.route(`**/api/v1/agent/runs/${runId}/resume`, route => route.fulfill({
    json: runJson(runId, sessionId, 'running'),
  }));

  await login(page, phone);
  await page.getByTitle('新建分析会话').click();
  await expect(page.getByRole('heading', { name: '恢复会话' })).toBeVisible();

  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill('分析品牌');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // paused 状态：Run 卡显示「已暂停」与「继续」按钮。
  const runCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(runCard.getByText('已暂停', { exact: true })).toBeVisible();
  const resumeButton = page.getByRole('button', { name: '继续' });
  await expect(resumeButton).toBeVisible();
  await resumeButton.click();

  phase = 'resumed';
  // 恢复后推进到终态：折叠摘要标注「分析完成」，「继续」按钮消失。
  await expect(page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 分析完成/ })).toBeVisible();
  await expect(page.getByRole('button', { name: '继续' })).toHaveCount(0);
});

// --------------------------------------------------------------------------- //
// 5. 四个 Quick 入口消失，顶部只保留智能会话/收藏
// --------------------------------------------------------------------------- //

test('the four legacy quick entries are absent from the workspace', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-quick';
  const session = sessionJson(sessionId, '统一会话');
  const detail = { ...session, messages: [], runs: [] };

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => route.fulfill({ json: [session] }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail }));
  await mockArtifactsEmpty(page, sessionId);

  await login(page, phone);

  for (const name of ['达人推荐', '活动评估', '小红书爆贴', '抖音爆贴']) {
    await expect(page.getByText(name, { exact: true })).toHaveCount(0);
    await expect(page.getByRole('tab', { name })).toHaveCount(0);
  }

  // 顶部工作区只保留智能会话与收藏（小视口先切到分析对话面板）。
  await ensureChatPane(page);
  await expect(page.getByRole('tab', { name: '智能会话' })).toBeVisible();
  await expect(page.getByRole('tab', { name: /已收藏/ })).toBeVisible();
});

// --------------------------------------------------------------------------- //
// 6. 登录恢复 + 会话软删除
// --------------------------------------------------------------------------- //

test('restores the session list after reload', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-restore';
  const session = sessionJson(sessionId, '恢复会话');
  const detail = { ...session, messages: [], runs: [] };

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => route.fulfill({ json: [session] }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail }));
  await mockArtifactsEmpty(page, sessionId);

  await login(page, phone);
  await expect(page.getByRole('button', { name: /选择会话 恢复会话/ })).toBeVisible();

  // reload 后经 refresh token 恢复登录态并重放会话列表（软删除不可见会话除外）。
  await page.reload();
  await expect(page.getByTitle('新建分析会话')).toBeVisible();
  await expect(page.getByRole('button', { name: /选择会话 恢复会话/ })).toBeVisible();
});

// C3：刷新/重进后历史 Run 不再是空壳终态卡——锚点 Run 走实时订阅恢复，
// 更早的历史 Run 经 events 端点一次性回放补齐步骤与 thinking。
test('restores historical run cards with replayed steps after reload', async ({ page }) => {
  const phone = await uniquePhone();
  const sessionId = 's-history';
  const session = sessionJson(sessionId, '多轮历史会话');
  const run1 = 'run-h1';
  const run2 = 'run-h2';
  const detail = {
    ...session,
    messages: [
      messageJson('m-user-1', 'user', '分析品牌声量', 1, run1),
      messageJson('m-ai-1', 'assistant', '品牌声量分析完成', 2, run1),
      messageJson('m-user-2', 'user', '再圈选一波达人', 3, run2),
      messageJson('m-ai-2', 'assistant', '圈选完成', 4, run2),
    ],
    runs: [runJson(run1, sessionId, 'completed'), runJson(run2, sessionId, 'completed')],
  };
  const run1Events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'thinking.started', payload: { attempt: 1 } },
    { seq: 3, event: 'thinking.delta', payload: { text: '复盘品牌声量' } },
    { seq: 4, event: 'thinking.completed' },
    { seq: 5, event: 'tool.started', payload: { internal_tool_name: 'brand_search' } },
    { seq: 6, event: 'tool.succeeded', payload: { internal_tool_name: 'brand_search', duration_ms: 900, points: 10 } },
    { seq: 7, event: 'run.completed', payload: { outcome: 'completed' } },
  ];
  const run2Events: SseEvent[] = [
    { seq: 1, event: 'run.started', payload: { run_kind: 'user' } },
    { seq: 2, event: 'tool.started', payload: { internal_tool_name: 'kol_search' } },
    { seq: 3, event: 'tool.succeeded', payload: { internal_tool_name: 'kol_search', duration_ms: 700, points: 10 } },
    { seq: 4, event: 'run.completed', payload: { outcome: 'completed' } },
  ];

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => route.fulfill({ json: [session] }));
  await page.route(`**/api/v1/agent/sessions/${sessionId}`, route => route.fulfill({ json: detail }));
  await mockArtifactsEmpty(page, sessionId);
  await page.route(`**/api/v1/agent/runs/${run1}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(run1, run1Events),
  }));
  await page.route(`**/api/v1/agent/runs/${run2}/events`, route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sseBody(run2, run2Events),
  }));

  await login(page, phone);
  await ensureChatPane(page);

  // 两张历史执行卡都带真实步数：run-h2（锚点）走实时订阅恢复，run-h1 走事件回放。
  const collapsedCards = page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 分析完成/ });
  await expect(collapsedCards).toHaveCount(2);

  // reload 后两张历史卡仍完整：步骤可见、thinking 折叠可回看。
  await page.reload();
  await ensureChatPane(page);
  await expect(page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 分析完成/ })).toHaveCount(2);
  await page.getByRole('button', { name: /执行卡 · 共 \d+ 步 · 分析完成/ }).first().click();
  const historyCard = page.getByRole('region', { name: '执行卡' }).first();
  await expect(historyCard.getByText('brand_search', { exact: true })).toBeVisible();
  await historyCard.getByRole('button', { name: '已思考' }).click();
  await expect(historyCard.getByText('复盘品牌声量', { exact: true })).toBeVisible();
});

test('soft-deletes a session and switches to the remaining one', async ({ page }) => {
  const phone = await uniquePhone();
  const s1 = sessionJson('s-del-1', '待删除会话');
  const s2 = sessionJson('s-del-2', '保留会话');
  const detailFor = (session: Record<string, unknown>) => ({
    ...session,
    messages: [],
    runs: [],
  });

  await mockWalletAndFavorites(page);
  await page.route('**/api/v1/agent/sessions', route => route.fulfill({ json: [s1, s2] }));
  await page.route('**/api/v1/agent/sessions/s-del-1', route => {
    if (route.request().method() === 'DELETE') return route.fulfill({ status: 204, body: '' });
    return route.fulfill({ json: detailFor(s1) });
  });
  await page.route('**/api/v1/agent/sessions/s-del-2', route => route.fulfill({ json: detailFor(s2) }));
  await mockArtifactsEmpty(page, 's-del-1');
  await mockArtifactsEmpty(page, 's-del-2');

  await login(page, phone);
  await expect(page.getByRole('button', { name: /选择会话 待删除会话/ })).toBeVisible();

  await page.getByRole('button', { name: /删除会话 待删除会话/ }).click();
  await page.getByRole('button', { name: '确认删除' }).click();

  // 软删除后从列表移除并切换到剩余会话。
  await expect(page.getByRole('button', { name: /选择会话 保留会话/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /选择会话 待删除会话/ })).toHaveCount(0);
});
