import { defineConfig, devices } from '@playwright/test';

const backendTestEnv = {
  APP_ENV: 'test',
  AUTH_MODE: 'mock',
  MYSQL_HOST: '127.0.0.1',
  MYSQL_PORT: '3306',
  MYSQL_DATABASE: 'kol_insight_test',
  MYSQL_USER: 'kol_test',
  MYSQL_PASSWORD: 'test-only-password',
  JWT_SECRET: 'test-only-jwt-secret-at-least-32-characters',
  // 仅为通过启动校验的占位值；E2E 数据由 page.route 注入，后台任务的真实
  // 供应商调用会异步失败，不影响断言。
  TENCENT_PLAN_API_KEY: 'test-only-tencent-key',
  DATATAP_MCP_TOKEN: 'test-only-datatap-token',
};


export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'desktop-1440',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'tablet-1024',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } },
    },
    {
      name: 'mobile-390',
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: [
    {
      command: 'backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000',
      url: 'http://127.0.0.1:8000/healthz',
      env: backendTestEnv,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
