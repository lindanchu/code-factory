// The paths of the UI check that the cases of the main fixture do not walk. Each test looks at what a check returned
// and at what finish() attached. The "web" is the design page itself, asked for by a query string and given seeded
// differences (see design-src/d1/Trang.html); a BAD screen does not fail a test here (ui_bad = column).
const path = require('path');
const { pathToFileURL } = require('url');
const { test, expect } = require('../autotest/test');
const { uiCheck } = require('../autotest/uicheck');
const { openDesign } = require('../autotest/uicheck/design');
const profile = require('../lib/ui-profile');

const PAGE = path.resolve(__dirname, '..', '..', 'design-src', 'd1', 'Trang.html');
const web = (query) => pathToFileURL(PAGE).href + (query ? `?${query}` : '');
const attached = (testInfo, name) => testInfo.attachments.filter(a => a.name === name);
const json = (testInfo, name) => JSON.parse(attached(testInfo, name)[0].body.toString());
const SIDE = { x: 0, y: 0, w: 200, h: 900 };   // the side panel of the page

test('the web shows a dialog that the design state does not have: one difference, the dialog', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=dialog'));
  const ui = uiCheck(browser, page, testInfo);
  const r = await ui.check('INITIAL', 1, 'PL-1');
  expect(r.level).toBe('BAD');
  expect(r.diffs.map(d => [d.element, d.type, d.design, d.actual])).toEqual([['Hộp thoại đang mở', 'MISSING_ELEMENT', 'không có', 'có']]);
  // framed where the dialog is: left 400, top 100, 402 wide (360 and its padding and border) on a 1440 x 900 screen
  expect(r.diffs[0].box.x).toBeCloseTo(100 * 400 / 1440, 1);
  expect(r.diffs[0].box.y).toBeCloseTo(100 * 100 / 900, 1);
  expect(r.diffs[0].box.w).toBeCloseTo(100 * 402 / 1440, 1);
  await ui.finish();
  expect(json(testInfo, 'ui-states')).toEqual([{ screen: 'INITIAL', step: 1, code: 'PL-1', scope: 'dialog', level: 'BAD', sent: true }]);
  expect(attached(testInfo, 'vung-lech')).toHaveLength(0);   // no failure image when BAD does not fail the case
  expect(json(testInfo, 'ui-info').package).toBe('d1-thử');  // a version is written without spaces
});

test('the design state has a dialog that the web does not show: its elements are missing', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=plain'));
  const ui = uiCheck(browser, page, testInfo);
  const r = await ui.check('INITIAL', 1, 'DG-1');
  expect(r.level).toBe('BAD');
  expect(r.diffs.length).toBeGreaterThan(0);
  expect(r.diffs.every(d => d.type === 'MISSING_ELEMENT' && d.design === 'có' && d.actual === 'không có')).toBe(true);
});

test('a dialog on a scrolled page: a difference is framed where it is on the screen', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=dialog&seed=1&scroll=300'));
  expect(await page.evaluate(() => window.scrollY)).toBe(300);
  const ui = uiCheck(browser, page, testInfo);
  const r = await ui.check('INITIAL', 1, 'DG-1');
  expect(r.diffs.map(d => [d.type, d.level])).toEqual([['FONT', 'BAD']]);
  // colours are named by the tokens of the package, whose prefix is read from its token file
  expect(r.diffs[0].design).toContain('màu chữ: #1B1F2A (--t-text-primary');
  expect(r.diffs[0].actual).toBe('#C63B2F (--t-color-danger)');
  // the paragraph is inside the dialog (fixed, top 100): its frame is there, not 300px lower
  expect(r.diffs[0].box.y).toBeGreaterThan(100 * 100 / 900);
  expect(r.diffs[0].box.y).toBeLessThan(100 * 220 / 900);
});

test('a menu is compared by its inside, wherever it opens', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=menu&mx=260&my=140'));   // the design state opens it at 700, 420
  const ui = uiCheck(browser, page, testInfo);
  expect((await ui.check('INITIAL', 1, 'MN-1')).level).toBe('OK');
  await page.goto(web('variant=menu&mx=260&my=140&bold=1'));
  const r = await ui.check('AFTER_ACTION', 2, 'MN-1');
  expect(r.level).toBe('BAD');
  const d = r.diffs.find(x => x.type === 'FONT');
  expect(d.element).toBe('Chữ "Khoá"');
  // framed where that item is on the web (in the menu at 260, 140), not where the design drew it
  expect(d.box.x).toBeGreaterThan(100 * 260 / 1440);
  expect(d.box.x).toBeLessThan(100 * 300 / 1440);
  expect(d.box.y).toBeGreaterThan(100 * 170 / 900);
  expect(d.box.y).toBeLessThan(100 * 215 / 900);
  await ui.finish();
  expect(json(testInfo, 'ui-states').map(s => [s.scope, s.level])).toEqual([['menu', 'OK'], ['menu', 'BAD']]);
  expect(Object.values(json(testInfo, 'ui-info').parts)).toEqual(Array(2).fill('Chỉ so menu đang mở; phần màn phía sau không so.'));
});

test('the part the profile keeps or drops', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=plain&side=Thanh%20kh%C3%A1c'));   // only the side panel differs from the design
  const ui = uiCheck(browser, page, testInfo);
  const all = await ui.check('INITIAL', 1, 'PL-1');
  expect(all.level).toBe('BAD');
  expect((await ui.check('AFTER_ACTION', 2, 'PL-1', { region: { drop: SIDE } })).level).toBe('OK');
  const kept = await ui.check('FINAL', 3, 'PL-1', { region: { keep: SIDE } });
  expect([kept.level, kept.diffs.length]).toEqual(['BAD', all.diffs.length]);
  await expect(ui.check('FINAL', 3, 'PL-1', { region: { keep: { x: 0 } } })).rejects.toThrow(/region của lib\/ui-profile\.js/);
  await ui.finish();
  const parts = json(testInfo, 'ui-info').parts;
  expect(parts['ui-1']).toBe('So cả màn đầu (1440×900).');
  expect(parts['ui-2']).toBe('So màn đầu (1440×900), trừ vùng rộng 200, cao 900 px tính từ điểm (0, 0).');
  expect(parts['ui-3']).toBe('Chỉ so vùng rộng 200, cao 900 px tính từ điểm (0, 0) của màn đầu (1440×900); phần còn lại không so.');
});

test('who is logged in, a note of the profile, a profile that says no, differences the profile takes out', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=plain&side=Kh%C3%A1c'));
  const ui = uiCheck(browser, page, testInfo);
  profile.calls.length = 0;
  expect((await ui.check('INITIAL', 1, 'PL-1', { noteIt: true, dropAll: true })).level).toBe('OK');
  expect(profile.calls).toEqual(['me', 'readData:page:từ trang', 'readData:page:từ trang']);   // read before and after the capture
  profile.calls.length = 0;
  const no = await ui.check('AFTER_ACTION', 2, 'PL-1', { me: 'người trong test', other: true });
  expect(profile.calls).toEqual(['readData:page:người trong test', 'readData:page:người trong test']);   // opts.me: the hook is not asked
  expect(no).toEqual({ level: null, diffs: [], notComparable: 'không so được: đã nói sẵn là không so được' });
  profile.calls.length = 0;
  expect((await ui.check('FINAL', 3, 'AN-1', { dropAll: true })).level).toBe('OK');   // a screen without data: nothing is read
  expect(profile.calls).toEqual([]);
  await ui.finish();
  const lines = attached(testInfo, 'ui-bo-qua')[0].body.toString().trim().split('\n');
  expect(lines[0]).toBe('[INITIAL] PL-1 — Cách đo: ghi chú của profile cho từ trang, thấy "Trang thử"');
  expect(lines.filter(l => l.endsWith(' — bỏ hết để thử')).length).toBeGreaterThan(1);
  expect(lines).toContain('[AFTER_ACTION] PL-1 — không so được: đã nói sẵn là không so được');
  expect(lines.every(l => /^\[(INITIAL|AFTER_ACTION|FINAL)\] [A-Z]+-\d+ /.test(l))).toBe(true);
});

test('one screen per step: the worst one, the later one on a tie', async ({ page, browser }, testInfo) => {
  const ui = uiCheck(browser, page, testInfo);
  const bad = web('variant=plain&side=Kh%C3%A1c'), ok = web('variant=plain');
  await page.goto(bad); expect((await ui.check('INITIAL', 1, 'PL-1')).level).toBe('BAD');        // step 1: BAD then OK
  await page.goto(ok); expect((await ui.check('AFTER_ACTION', 1, 'PL-1')).level).toBe('OK');
  await page.goto(ok); expect((await ui.check('INITIAL', 2, 'PL-1')).level).toBe('OK');           // step 2: OK then BAD
  await page.goto(bad); expect((await ui.check('FINAL', 2, 'PL-1')).level).toBe('BAD');
  await page.goto(ok);                                                                            // step 3: three alike
  for (const screen of ['INITIAL', 'AFTER_ACTION', 'FINAL']) await ui.check(screen, 3, 'PL-1');
  await ui.finish();
  expect(json(testInfo, 'ui_checks').map(c => [c.step, c.name.split(' · ')[0], c.level, c.screenshot]))
    .toEqual([[1, 'Ban đầu', 'BAD', 'ui-1'], [2, 'Màn cuối', 'BAD', 'ui-4'], [3, 'Màn cuối', 'OK', 'ui-7']]);
  expect(json(testInfo, 'ui-states').map(s => s.sent)).toEqual([true, false, false, true, false, false, true]);
  expect(testInfo.attachments.filter(a => a.contentType === 'image/png').map(a => a.name)).toEqual(['ui-1', 'design-1', 'ui-4', 'design-4', 'ui-7', 'design-7']);
});

test('a design page that stops on an error is not compared; a refused input, a wrong call or a profile without data is an error', async ({ page, browser }, testInfo) => {
  await page.goto(web('variant=plain'));
  const ui = uiCheck(browser, page, testInfo);
  const r = await ui.check('INITIAL', 1, 'CR-1');
  expect([r.level, r.notComparable]).toEqual([null, 'không so được: trang design không vẽ được màn này với dữ liệu đọc từ web']);
  expect(r.designErrors.join(' ')).toMatch(/TypeError/);
  await expect(ui.check('AFTER_ACTION', 2, 'RF-1')).rejects.toThrow(/^trang design "Trang\.html": Không mở được trang: Error: màn này không nhận dữ liệu này/);
  await expect(ui.check('AFTER_ACTION', 2, 'ND-1')).rejects.toThrow(/readData của lib\/ui-profile\.js không trả về dữ liệu cho trạng thái ND-1/);
  await expect(ui.check('AFTER_ACTION', 2, 'XX-9')).rejects.toThrow(/gói design d1-thử không có trạng thái XX-9/);
  await expect(ui.check('LATER', 2, 'PL-1')).rejects.toThrow(/INITIAL, AFTER_ACTION hoặc FINAL/);
  await expect(ui.check('FINAL', 0, 'PL-1')).rejects.toThrow(/số nguyên từ 1/);
  await ui.finish();
  await ui.finish();   // a second finish attaches nothing more
  expect(attached(testInfo, 'ui-states')).toHaveLength(1);
  expect(attached(testInfo, 'ui_checks')).toHaveLength(0);
});

test('a design file that is not there is an error at once', async ({ browser }) => {
  const missing = path.join(path.dirname(PAGE), 'Không có.html');
  const started = Date.now();
  await expect(openDesign(browser, missing, {}, { width: 800, height: 600 })).rejects.toThrow(/ERR_FILE_NOT_FOUND/);
  expect(Date.now() - started).toBeLessThan(5000);
});

test('a look the design gives a meaning is checked by its token, where the screen cannot be compared', async ({ page }) => {
  const { expectLook } = require('../autotest/style');
  await page.goto(web('variant=dialog'));
  const line = page.getByText('Bạn chắc chắn muốn tiếp tục?');
  await expectLook(line, { color: 'text-primary' }, 'Câu hỏi của hộp thoại');          // the token without its prefix
  await expectLook(line, { color: '--t-text-primary' }, 'Câu hỏi của hộp thoại');      // and with it
  const title = page.getByRole('heading', { name: 'Xác nhận' });
  await expectLook(title, { 'font-size': '18px', 'font-weight': 700 }, 'Tiêu đề hộp thoại');   // plain CSS values
  await expect(expectLook(title, { 'font-size': '22px' }, 'Tiêu đề hộp thoại'))
    .rejects.toThrow('Tiêu đề hộp thoại: cỡ chữ theo design là 22px, web đang là 18px.');
  await expect(expectLook(line, { color: '--t-khong-co' }, 'Câu hỏi')).rejects.toThrow(/gói design không có token --t-khong-co/);
  await expect(expectLook(line, { color: 'text-primary' })).rejects.toThrow(/thiếu tên phần tử/);

  // the seeded difference: the line is drawn in the danger colour
  await page.goto(web('variant=dialog&seed=1'));
  await expect(expectLook(line, { color: 'text-primary' }, 'Câu hỏi của hộp thoại')).rejects.toThrow(
    'Câu hỏi của hộp thoại: màu chữ theo design là rgb(27, 31, 42) (--t-text-primary), web đang là rgb(198, 59, 47) (--t-color-danger).');
});

test('a placeholder whose colour the design leaves to the browser is not held to a colour', async ({ page }) => {
  const extract = require('../autotest/uicheck/extract');
  const E = require('../autotest/uicheck/engine');
  const field = (style) => `<style>${style}</style><label>Email <input placeholder="ten@qa.test"></label>`;
  const nodes = async (html) => { await page.setContent(html); return page.evaluate(extract, {}); };
  const colourDiffs = (D, R) => E.compare(D, R, E.matchNodes(D, R), { focus: false }).diffs.filter(d => d.prop === 'placeholderColor');
  // the design sets no colour for its placeholder: whatever the web's is, nothing to hold it to
  const plain = await nodes(field(''));
  const red = await nodes(field('input::placeholder{color:rgb(200,0,0)}'));
  expect(colourDiffs(plain, red)).toEqual([]);
  // the design gives the placeholder a colour of its own: the web is held to it
  const grey = await nodes(field('input::placeholder{color:rgb(120,120,120)}'));
  expect(colourDiffs(grey, red).length).toBe(1);
});
