import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  workers: 1,
  use: {
    baseURL: process.env.DEMO_WEB_URL,
    screenshot: 'only-on-failure',
  },
});
