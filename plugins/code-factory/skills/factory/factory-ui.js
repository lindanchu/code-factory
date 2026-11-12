// factory-ui.js: a small harness for Playwright UI checks on a factory job's running app.
//
// A job's ui/check.js uses it like this:
//
//   const ui = require(process.env.FACTORY_UI);
//   ui.run(async (t) => {
//     await t.goto('/login');                       // relative to the job's app URL
//     await t.step('AC1: admin can sign in', async () => {
//       await t.page.fill('#email', 'admin@example.test');
//       await t.page.fill('#password', 'secret');
//       await t.page.click('button[type=submit]');
//       await t.page.waitForURL('**/dashboard');
//     });
//     await t.snap('dashboard');                    // every viewport + accessibility scan
//   });
//
// Everything the harness sees is written to FACTORY_UI_REPORT (JSON): steps, screenshots,
// console errors, failed requests, accessibility violations. fx decides what blocks.
// Playwright and axe-core come from the factory's own tools folder, never from the project.

'use strict';
const fs = require('fs');
const path = require('path');

function load(name) {
  const dirs = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
  for (const d of dirs) {
    try { return require(path.join(d, name)); } catch (e) { /* next */ }
  }
  return require(name);
}

function axePath() {
  const dirs = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
  for (const d of dirs) {
    const p = path.join(d, 'axe-core', 'axe.min.js');
    if (fs.existsSync(p)) return p;
  }
  try { return require.resolve('axe-core/axe.min.js'); } catch (e) { return null; }
}

function slug(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'shot';
}

async function run(fn) {
  const base = (process.env.BASE_URL || '').replace(/\/$/, '');
  const shots = process.env.FACTORY_UI_SHOTS || path.resolve('ui-shots');
  const reportPath = process.env.FACTORY_UI_REPORT || path.resolve('ui-report.json');
  const viewports = JSON.parse(process.env.FACTORY_UI_VIEWPORTS || '[[1280,800],[390,844]]');
  const blockStatus = parseInt(process.env.FACTORY_UI_BLOCK_STATUS || '500', 10);
  fs.mkdirSync(shots, { recursive: true });

  const report = { base, steps: [], shots: [], console: [], requests: [], a11y: [], document: null, error: null };
  const save = () => fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

  let browser;
  try {
    const { chromium } = load('playwright');
    browser = await chromium.launch();
  } catch (e) {
    // No browser is a missing tool, not a failed check: fx reports it as skipped.
    report.browser_missing = String(e && e.message || e).split('\n')[0];
    save();
    return;
  }
  const context = await browser.newContext({ viewport: { width: viewports[0][0], height: viewports[0][1] } });
  const page = await context.newPage();

  page.on('console', (m) => {
    if (m.type() === 'error') report.console.push({ text: m.text(), url: page.url() });
  });
  page.on('pageerror', (e) => report.console.push({ text: String(e && e.message || e), url: page.url(), uncaught: true }));
  page.on('requestfailed', (r) => {
    const f = r.failure();
    if (f && /ERR_ABORTED/.test(f.errorText)) return; // navigation away, not an error
    report.requests.push({ url: r.url(), method: r.method(), failure: f ? f.errorText : 'failed' });
  });
  page.on('response', (r) => {
    if (r.status() >= 400) {
      report.requests.push({ url: r.url(), method: r.request().method(), status: r.status(),
                             blocking: r.status() >= blockStatus });
    }
  });

  const axe = axePath();
  let axeSource = null;
  const t = {
    page, context, browser, base,
    url: (p) => (/^https?:/.test(p) ? p : base + (p.startsWith('/') ? p : '/' + p)),
    async goto(p, opts) {
      const res = await page.goto(t.url(p), Object.assign({ waitUntil: 'networkidle' }, opts || {}));
      if (res && !report.document) {
        report.document = { url: res.url(), status: res.status(), headers: await res.allHeaders() };
      }
      return res;
    },
    async snap(name, opts) {
      const n = slug(name);
      for (const [w, h] of viewports) {
        await page.setViewportSize({ width: w, height: h });
        await page.waitForTimeout(150);
        const file = path.join(shots, `${n}-${w}x${h}.png`);
        await page.screenshot({ path: file, fullPage: !(opts && opts.fullPage === false) });
        report.shots.push({ name, viewport: `${w}x${h}`, file, url: page.url() });
      }
      await page.setViewportSize({ width: viewports[0][0], height: viewports[0][1] });
      if (axe && !(opts && opts.a11y === false)) {
        // Evaluated through the DevTools protocol, so the page's CSP neither blocks axe nor is relaxed.
        if (!(await page.evaluate(() => typeof window.axe !== 'undefined'))) {
          await page.evaluate(axeSource || (axeSource = fs.readFileSync(axe, 'utf8')));
        }
        const res = await page.evaluate(async () => {
          // eslint-disable-next-line no-undef
          const r = await axe.run(document, { resultTypes: ['violations'] });
          return r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl,
            nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')) }));
        });
        for (const v of res) report.a11y.push(Object.assign({ page: name, url: page.url() }, v));
      } else if (!axe) {
        report.a11y_skipped = 'axe-core not installed (fx tools install playwright)';
      }
      save();
    },
    async step(name, body) {
      const s = { name, ok: true };
      try { await body(); } catch (e) {
        s.ok = false; s.error = String(e && e.message || e).split('\n').slice(0, 6).join('\n');
        try {
          const file = path.join(shots, `failed-${slug(name)}.png`);
          await page.screenshot({ path: file, fullPage: true });
          s.shot = file;
        } catch (e2) { /* page may be gone */ }
      }
      report.steps.push(s);
      save();
      return s.ok;
    },
  };

  try {
    await fn(t);
  } catch (e) {
    report.error = String(e && e.stack || e).split('\n').slice(0, 8).join('\n');
  } finally {
    save();
    await browser.close();
  }
}

module.exports = { run };
