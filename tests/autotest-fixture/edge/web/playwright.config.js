// The edge tests of the UI check: one browser, one worker, no retry (each test asserts for itself and must pass).
const { defineConfig, devices } = require('@playwright/test');

const out = process.env.RESULTS_DIR || 'results/latest';

module.exports = defineConfig({
  testDir: './tests',
  retries: 0,
  workers: 1,
  timeout: 30_000,
  reporter: [['./autotest/reporter.js'], ['line']],
  outputDir: `${out}/artifacts`,
  use: { trace: 'off', screenshot: 'off', video: 'off' },
  projects: [
    { name: 'chromium-1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
