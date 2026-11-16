// The cases of the made-up feature DEM (plans/DEM.json), written as the auto test writes them for a project:
// the runtime's test, the declared steps of the plan, one UI check per case.
const { test, expect } = require('../../autotest/test');
const { title, step } = require('../../autotest/plan');
const { uniq, EMPTY_STATE } = require('../../autotest/data');
const { uiCheck } = require('../../autotest/uicheck');
const { url } = require('../../lib/product');
const profile = require('../../lib/ui-profile');

const addDialog = (page) => page.getByRole('dialog', { name: 'Thêm sách' });

// Every screen matches its design: a page that shows no data, the list, a dialog.
test(title('WEB-DEM-01'), async ({ page, browser }, testInfo) => {
  const C = 'WEB-DEM-01';
  const ui = uiCheck(browser, page, testInfo);
  await step(C, 0, async () => {
    await page.goto(url('help.html'));
    await expect(page.getByRole('heading', { name: 'Trợ giúp' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Quay lại Kho sách' })).toBeVisible();
    await ui.check('INITIAL', 1, 'HP-1');
  });
  await step(C, 1, async () => {
    await page.getByRole('link', { name: 'Quay lại Kho sách' }).click();
    await expect(page.getByRole('row')).toHaveCount(5);   // the header and 4 books
    await expect(page.getByText('Tổng số: 4 đầu sách')).toBeVisible();
    await ui.check('AFTER_ACTION', 2, 'BK-1');
  });
  await step(C, 2, async () => {
    await page.getByRole('button', { name: 'Thêm sách' }).click();
    await expect(addDialog(page)).toBeVisible();
    for (const label of ['Mã sách', 'Tên sách', 'Tác giả']) await expect(addDialog(page).getByLabel(label)).toHaveValue('');
    await ui.check('FINAL', 3, 'BK-2');
  });
  await ui.finish();
});

// The product is opened a little off its design (seed=minor): the dialog is MINOR, the case passes. Step 2 has two
// screens: one is sent, the later one on a tie. Step 3 compares the list after the web's data changed.
test(title('WEB-DEM-02'), async ({ page, browser }, testInfo) => {
  const C = 'WEB-DEM-02';
  const typed = { code: uniq('KS'), name: 'Lão Hạc', author: 'Nam Cao' };
  const ui = uiCheck(browser, page, testInfo);
  await step(C, 0, async () => {
    await page.goto(url('index.html', 'seed=minor'));
    await expect(page.getByRole('row')).toHaveCount(5);
    await ui.check('INITIAL', 1, 'BK-1');
  });
  await step(C, 1, async () => {
    await page.getByRole('button', { name: 'Thêm sách' }).click();
    await expect(addDialog(page)).toBeVisible();
    await ui.check('AFTER_ACTION', 2, 'BK-2');
    await addDialog(page).getByLabel('Mã sách').fill(typed.code);
    await addDialog(page).getByLabel('Tên sách').fill(typed.name);
    await addDialog(page).getByLabel('Tác giả').fill(typed.author);
    await expect(addDialog(page).getByLabel('Mã sách')).toHaveValue(typed.code);
    await expect(addDialog(page).getByLabel('Tác giả')).toHaveValue(typed.author);
    await ui.check('FINAL', 2, 'BK-2', { typed });
  });
  await step(C, 2, async () => {
    await addDialog(page).getByRole('button', { name: 'Lưu' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('row', { name: typed.code })).toContainText('Lão Hạc');
    await expect(page.getByText('Tổng số: 5 đầu sách')).toBeVisible();
    await ui.check('FINAL', 3, 'BK-1');
  });
  await ui.finish();
});

// The product is opened far from its design (seed=bad): the list is BAD, its step fails softly, the next steps still
// run, and the screenshot with the BAD differences framed is attached.
test(title('WEB-DEM-03'), async ({ page, browser }, testInfo) => {
  const C = 'WEB-DEM-03';
  const ui = uiCheck(browser, page, testInfo);
  await step(C, 0, async () => {
    await page.goto(url('index.html', 'seed=bad'));
    await expect(page.getByRole('row')).toHaveCount(5);
    await ui.check('INITIAL', 1, 'BK-1');
  });
  await step(C, 1, async () => {
    await page.getByRole('button', { name: 'Thêm sách' }).click();
    await expect(addDialog(page)).toBeVisible();
    await ui.check('AFTER_ACTION', 2, 'BK-2');
  });
  await step(C, 2, async () => {
    await addDialog(page).getByLabel('Mã sách').fill('KS-900');
    await expect(addDialog(page).getByLabel('Mã sách')).toHaveValue('KS-900');
    await ui.check('FINAL', 3, 'BK-2', { typed: { code: 'KS-900', name: '', author: '' } });
  });
  await ui.finish();
});

// A dialog about one object. The design draws the first book of the data: a check that is not told which book the
// web shows is not compared (and never fails the case); told, it moves that book first and compares.
// The same case counts how often a check reads the web's data: once before and once after the page is captured, and
// again, with a new capture, while two reads differ (opts.changing, see lib/ui-profile.js): at most 3 captures.
test(title('WEB-DEM-04'), async ({ page, browser }, testInfo) => {
  const C = 'WEB-DEM-04';
  const detail = page.getByRole('dialog', { name: 'Tắt đèn' });
  const ui = uiCheck(browser, page, testInfo);
  const reads = async (check) => { const before = profile.reads; await check(); return profile.reads - before; };
  await step(C, 0, async () => {
    await page.goto(url('index.html'));
    await expect(page.getByRole('row')).toHaveCount(5);
    // the data changes once while the screen is captured: captured again, then two reads agree
    expect(await reads(() => ui.check('INITIAL', 1, 'BK-1', { changing: 1 }))).toBe(3);
  });
  await step(C, 1, async () => {
    await page.getByRole('row', { name: /Tắt đèn/ }).click();
    await expect(detail).toBeVisible();
    await expect(detail).toContainText('KS-003');
    await expect(detail).toContainText('7');
    expect(await reads(() => ui.check('AFTER_ACTION', 2, 'BK-3'))).toBe(2);
  });
  await step(C, 2, async () => {
    await expect(detail).toContainText('Còn hàng');
    // the data never stops changing: three captures and no more, the last read is the one drawn
    expect(await reads(() => ui.check('FINAL', 3, 'BK-3', { book: 'KS-003', changing: 9 }))).toBe(4);
    await ui.check('FINAL', 3, 'RP-1');   // a page of the package that does not take data from outside yet
  });
  await ui.finish();
});

// A functional failure: the product saves a book whose code already exists. Step 2 fails, step 3 never runs. The test
// opens a context of its own, as a test of several users does: the runtime films it and attaches the film.
test(title('WEB-DEM-05'), async ({ browser }, testInfo) => {
  const C = 'WEB-DEM-05';
  const ctx = await browser.newContext({ storageState: EMPTY_STATE });
  const page = await ctx.newPage();
  const ui = uiCheck(browser, page, testInfo);   // no finish(): the case fails at step 3, the runtime finishes it
  await step(C, 0, async () => {
    await page.goto(url('index.html'));
    await page.getByRole('button', { name: 'Thêm sách' }).click();
    await expect(addDialog(page)).toBeVisible();
    await ui.check('INITIAL', 1, 'BK-2');
  });
  await step(C, 1, async () => {
    await addDialog(page).getByLabel('Mã sách').fill('KS-001');
    await addDialog(page).getByLabel('Tên sách').fill('Bản sao');
    await addDialog(page).getByRole('button', { name: 'Lưu' }).click();
    await expect(page.getByText('Mã sách này đã có.')).toBeVisible();
    await expect(addDialog(page)).toBeVisible();
  });
  await step(C, 2, async () => {
    await addDialog(page).getByRole('button', { name: 'Huỷ' }).click();
    await expect(page.getByRole('row')).toHaveCount(5);
  });
});

// A menu: only the open menu is compared. After sorting, the design is drawn with the rows in the web's new order.
test(title('WEB-DEM-08'), async ({ page, browser }, testInfo) => {
  const C = 'WEB-DEM-08';
  const order = async (...codes) => { for (const [i, code] of codes.entries()) await expect(page.getByRole('row').nth(i + 1)).toContainText(code); };
  const ui = uiCheck(browser, page, testInfo);
  await step(C, 0, async () => {
    await page.goto(url('index.html'));
    await order('KS-001', 'KS-002', 'KS-003', 'KS-004');
    await ui.check('INITIAL', 1, 'BK-1');
  });
  await step(C, 1, async () => {
    await page.getByRole('button', { name: 'Sắp xếp' }).click();
    await expect(page.getByRole('menu')).toBeVisible();
    await expect(page.getByRole('menuitem')).toHaveText(['Theo mã', 'Theo tên sách', 'Theo số lượng']);
    await ui.check('AFTER_ACTION', 2, 'BK-4');
  });
  await step(C, 2, async () => {
    await page.getByRole('menuitem', { name: 'Theo số lượng' }).click();
    await expect(page.getByRole('menu')).toHaveCount(0);
    await order('KS-001', 'KS-003', 'KS-004', 'KS-002');
    await ui.check('FINAL', 3, 'BK-1');
  });
  await ui.finish();
});
