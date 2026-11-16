// The fixture's Playwright config: one browser, one worker, the product opened as local files (no server, no network).
const { defineConfig, devices } = require('@playwright/test');

// Each run writes its own folder (run.sh sets RESULTS_DIR); the reporter of the runtime reads the same variable.
const out = process.env.RESULTS_DIR || 'results/latest';

module.exports = defineConfig({
  testDir: './tests',
  // A failed test runs once more: the reporter keeps both attempts.
  retries: 1,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 1_500 },
  // seen-reporter comes after the runtime's reporter on purpose: it looks at what that one has already written
  reporter: [['./autotest/reporter.js'], ['./lib/seen-reporter.js'], ['line']],
  outputDir: `${out}/artifacts`,
  use: {
    locale: 'vi-VN',
    timezoneId: 'Asia/Ho_Chi_Minh',
    trace: 'off',
    screenshot: 'off',
    // off here: the runtime's test.js films every context a test opens and keeps the film of a failed test
    video: 'off',
  },
  projects: [
    { name: 'chromium-1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
