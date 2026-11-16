// The design reference: an exported page of the design package, opened as a local file with NO network, its state and
// its data given from outside in the address (#dc=<json>). Nothing inside the page is touched: no runtime object, no
// script. What a design package promises (and all this file relies on):
//   <Page>.html#dc=<encodeURIComponent(JSON)>   the JSON holds the props of a state and `data` (shape of <Page>.data.json)
//   #dc-root                                    the element the screen is drawn in
//   "Không mở được trang…"                      the text a page shows when it refuses its input
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = '#dc-root';
const REFUSED = 'Không mở được trang';
const MAX_URL = 2 * 1024 * 1024;   // a browser does not open a longer address

const url = (file, input) => pathToFileURL(file).href + (input ? '#dc=' + encodeURIComponent(JSON.stringify(input)) : '');

// in the page: the screen is there (something is drawn in the root), or the page says it refuses its input
const READY = ([rootSel, refused]) => {
  if (document.body && document.body.textContent.trim().startsWith(refused)) return true;
  const root = document.querySelector(rootSel);
  return !!root && (root.innerText.trim().length > 0 || !!root.querySelector('svg, img, canvas, input, button, select, textarea'));
};
// in the page: fonts loaded, then the screen has stopped changing for three frames in a row (a page draws in several
// passes: a menu or a dialog of the state can come one pass after the screen under it); never longer than 5 seconds
const SETTLED = (rootSel) => new Promise((done) => {
  const root = document.querySelector(rootSel) || document.body;
  setTimeout(() => done(null), 5000);
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => {
    let last = -1, same = 0, n = 0;
    const tick = () => {
      const len = root.innerHTML.length;
      same = len === last ? same + 1 : 0;
      last = len;
      if (same >= 2 || ++n > 180) return done(null);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
});
// a message of the page's console that is a script error (a failed download or a warning is not)
const SCRIPT_ERROR = /^(Uncaught\s+)?(\(in promise\)\s+)?[A-Za-z]*(Error|Exception)\b/;

/**
 * Open a design page in a new context of this browser.
 *   input     the object given to the page (props of the state, data), or null for the page as it ships
 *   viewport  { width, height }, the one of the page it is compared with
 *   context   more options of the context, taken from the page it is compared with (locale, timezoneId, ...)
 * Returns { page, close, blocked, errors }: blocked = the addresses the page asked the network for (all refused),
 * errors = the script errors of the page while it drew (a screen drawn with an error is not a reference).
 * Throws when the page refuses its input, or draws nothing.
 */
async function openDesign(browser, file, input, viewport, context = {}) {
  const address = url(file, input);
  if (address.length > MAX_URL) throw new Error(`trang design "${path.basename(file)}": dữ liệu quá lớn để đưa vào địa chỉ trang (${address.length} ký tự)`);
  // the design page is not part of a case's video (test.js records every context made while this is not set)
  const before = globalThis.__noVideo;
  globalThis.__noVideo = true;
  let ctx;
  try {
    ctx = await browser.newContext({ ...context, viewport, javaScriptEnabled: true, offline: false, storageState: { cookies: [], origins: [] } });
  } finally { globalThis.__noVideo = before; }
  const blocked = [], errors = [];
  try {
    const page = await ctx.newPage();
    await page.route(/^https?:/, (r) => { blocked.push(r.request().url().slice(0, 100)); return r.abort(); });
    page.on('pageerror', e => errors.push(String(e).split('\n')[0].slice(0, 200)));
    page.on('console', m => { if (m.type() === 'error' && SCRIPT_ERROR.test(m.text())) errors.push(m.text().split('\n')[0].slice(0, 200)); });
    await page.goto(address, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(READY, [ROOT, REFUSED], { timeout: 30000 })
      .catch((e) => { throw (e && e.name === 'TimeoutError' ? new Error(`trang design "${path.basename(file)}": sau 30 giây vẫn chưa vẽ gì trong ${ROOT}`) : e); });
    const stop = await page.evaluate((refused) => { const t = document.body.textContent.trim(); return t.startsWith(refused) ? t.slice(0, 200) : ''; }, REFUSED);
    if (stop) throw new Error(`trang design "${path.basename(file)}": ${stop}`);
    await page.evaluate(SETTLED, ROOT);
    return { page, blocked, errors, close: () => ctx.close() };
  } catch (e) {
    await ctx.close().catch(() => {});
    throw e;
  }
}

module.exports = { openDesign, url, ROOT };
