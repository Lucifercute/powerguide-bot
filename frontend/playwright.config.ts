import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8765', channel: 'chrome', headless: true, screenshot: 'only-on-failure' },
  webServer: {
    command: '../.venv/bin/python ../scripts/test_miniapp_server.py',
    url: 'http://127.0.0.1:8765/api/v1/config',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
