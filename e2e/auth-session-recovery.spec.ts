import { expect, test } from '@playwright/test';


test('mock user receives points and restores a complete session after reload', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8);
  const phone = `139${suffix}`;
  const campaignName = `夏季防晒选人${suffix}`;
  const followUp = `请补充预算匹配依据-${suffix}`;

  await page.goto('/');
  await page.getByPlaceholder('请输入11位中国手机号码').fill(phone);
  await page.getByRole('button', { name: '获取验证码' }).click();
  await page.getByRole('button', { name: '立即安全登录' }).click();

  await expect(page.getByText('1,000 / 5,000 点')).toBeVisible();
  await expect(page.getByTitle('管理员控制台')).toHaveCount(0);

  await page.getByTitle('新建分析会话').click();
  await page.getByPlaceholder('例如：雅诗兰黛').fill('示例品牌');
  await page.getByPlaceholder('例如：双11抗老宣发').fill(campaignName);
  await page.getByPlaceholder('例如：美妆护肤').fill('防晒护肤');
  await page.getByPlaceholder('例如：25-35 岁一线城市女性').fill('18-30 岁通勤女性');
  await page.getByRole('button', { name: '立即创建' }).click();

  await expect(page.getByText(campaignName, { exact: false }).first()).toBeVisible();
  await page.getByPlaceholder(/输入消息并向 AI 分析师提问/).fill(followUp);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByText(followUp, { exact: true }).last()).toBeVisible();

  await page.reload();

  await expect(page.getByText(campaignName, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(followUp, { exact: true }).last()).toBeVisible();
  await expect(page.getByText('1,000 / 5,000 点')).toBeVisible();
});
