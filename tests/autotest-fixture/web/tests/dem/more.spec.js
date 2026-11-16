// Cases of plans/DEM.p2.json (a second part of the same feature): what the reporter must keep of a test that is
// skipped with a reason, and of a test that fails once and passes on its retry.
const { test, expect } = require('../../autotest/test');
const { title, step } = require('../../autotest/plan');
const { url } = require('../../lib/product');

test(title('WEB-DEM-06'), async () => {
  test.skip(true, 'Chờ ticket KS-12: sản phẩm chưa có nút "Xuất báo cáo"');
  await step('WEB-DEM-06', 0, async () => {});
});

// The plan key carries a suffix (a part of case WEB-DEM-07 that needs another environment).
test(title('WEB-DEM-07-ENV'), async ({ page }, testInfo) => {
  const C = 'WEB-DEM-07-ENV';
  await step(C, 0, async () => {
    await page.goto(url('help.html'));
    expect(testInfo.retry, 'lần chạy đầu lỗi có chủ ý, lần chạy lại thì đạt').toBeGreaterThan(0);
    await expect(page.getByRole('heading', { name: 'Trợ giúp' })).toBeVisible();
  });
});
