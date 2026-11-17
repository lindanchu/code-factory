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
//     await t.design('PD-6', { project: 'demo' });  // measured against the design package's state PD-6
//   });
//
// Everything the harness sees is written to FACTORY_UI_REPORT (JSON): steps, screenshots,
// console errors, failed requests, accessibility violations, design measurements. fx decides what blocks.
// Playwright and axe-core come from the factory's own tools folder, never from the project.
//
// A mobile project (FACTORY_UI_MOBILE, written by fx): the same script runs once per virtual phone, the app started
// with its data cleared, and `t` drives the app instead of a page (the auto test's runtime app.js):
//
//   ui.run(async (t) => {
//     await t.step('AC1: the intro opens on step 0', async () => {
//       await t.see('Next');                        // a text or label on the screen
//       await t.notSee('Skip');
//     });
//     await t.tap('Next');                          // t.type('Email', 'a@b.test'), t.swipe('left'), t.back(),
//     await t.snap('step-1');                       // t.restart(), t.background(3), t.openLink('app://x'),
//     await t.design('IN-2', {});                   // t.answerPermission(true); t.app is the WebdriverIO session,
//   });                                             // t.device the phone ({ key, platform, name, os, screen })
//
// A desktop app (FACTORY_UI_DESKTOP, written by fx; a Tauri 2 app): the same script runs once as the website (`web`,
// when the front end is shipped as one too) and once as the app's window (`desktop`): Chromium at the window's size,
// Tauri's IPC answered by mocks (factory-desktop.js). Then fx runs it again on the real app (`app`, FACTORY_UI_APPEND):
// built on Windows and driven over WebView2's DevTools protocol, its Rust side answering. Steps, screenshots and errors
// are named by the way they ran:
//
//   ui.run(async (t) => {
//     await t.goto('/');
//     if (t.desktop) {                              // null on the website; { name, version, os, real, ... } in the app
//       if (t.ipc.mocked) t.ipc.handle('load_settings', () => ({ theme: 'dark' }));   // over the mocks file
//       await t.step('AC3: settings are saved on the machine', async () => {
//         await t.page.click('text=Save');
//         if (!t.ipc.calls('save_settings').length) throw new Error('save_settings was not called');
//       });
//       await t.ipc.emit('sync-done', { count: 3 });  // an event from the Rust side
//       await t.restart();                          // the app closed and opened again, its data kept
//     }
//   });
//
// t.design(code, opts) is the auto test's comparer: the page as a user sees it now, element by element, against the
// design page of state `code`, drawn with the data the page shows (read by the project's own profile, which also
// takes `opts`). It runs at the auto test's screen size and with its thresholds. FACTORY_UI_DESIGN, written by fx,
// says where all of that is; when something is missing, or a screen cannot be compared, the result says why, and a
// person looks at the design by eye. It never throws: the script goes on.

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

// What fx says about measuring against the design: { packages: [{ root, version, codes }], viewport, why }. Each root is
// a folder laid out like a test repository (autotest.json, web/autotest/uicheck, web/lib/ui-profile*) for one design
// package, so the comparer runs as it is; codes are the states that package has.
function designSetup() {
  const file = process.env.FACTORY_UI_DESIGN;
  if (!file) return { why: 'no design setup for this run' };
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) {
    return { why: 'cannot read the design setup: ' + String(e && e.message || e).split('\n')[0] };
  }
}

// A desktop app (FACTORY_UI_DESKTOP, written by fx): what factory-desktop.js needs, or null for a web.
function desktopSetup() {
  const file = process.env.FACTORY_UI_DESKTOP;
  if (!file) return null;
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) {
    return { variants: ['desktop'], viewports: [[800, 600]], app: {}, error: 'cannot read the desktop setup: ' + String(e && e.message || e).split('\n')[0] };
  }
}

async function run(fn) {
  if (process.env.FACTORY_UI_MOBILE) return runMobile(fn);
  const base = (process.env.BASE_URL || '').replace(/\/$/, '');
  const shots = process.env.FACTORY_UI_SHOTS || path.resolve('ui-shots');
  const reportPath = process.env.FACTORY_UI_REPORT || path.resolve('ui-report.json');
  const webViewports = JSON.parse(process.env.FACTORY_UI_VIEWPORTS || '[[1280,800],[390,844]]');
  const blockStatus = parseInt(process.env.FACTORY_UI_BLOCK_STATUS || '500', 10);
  const desktop = desktopSetup();
  fs.mkdirSync(shots, { recursive: true });

  // FACTORY_UI_APPEND: a second run of the same script (the real desktop app, perhaps from Windows' Node) adds to the
  // report of the first
  let report = null;
  if (process.env.FACTORY_UI_APPEND) {
    try { report = JSON.parse(fs.readFileSync(reportPath, 'utf8')); } catch (e) { report = null; }
  }
  report = report || { base, steps: [], shots: [], console: [], requests: [], a11y: [], design: [], document: null, error: null };
  if (desktop) {
    report.desktop = Object.assign(report.desktop || {}, { framework: desktop.framework || 'tauri', app: desktop.app,
                       conf: desktop.conf || null, mocks: desktop.mocks_name || null, viewports: desktop.viewports });
    report.desktop.variants = Array.from(new Set((report.desktop.variants || []).concat(desktop.variants || [])));
    report.ipc = report.ipc || { calls: [], missing: [] };
    if (desktop.error) report.desktop.error = desktop.error;
  }
  const setup = designSetup();
  const engines = {};   // the comparer of each package, loaded on its first t.design
  const save = () => fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

  // A desktop app's script runs once as the website (when the front end is shipped as one too) and once as the app's
  // window: Tauri's IPC answered by mocks, at the window's size; then, in a run of its own, on the real app (`app`).
  // A web runs once, as it always did.
  const variants = desktop ? (desktop.variants && desktop.variants.length ? desktop.variants : ['desktop']) : [null];
  const tag = (variant) => (variant ? `[${variant}] ` : '');
  let browser = null;
  let chromium;
  try {
    chromium = load('playwright').chromium;
    // the real app is a browser of its own (WebView2): no Chromium of Playwright for it
    if (variants.some((v) => v !== 'app')) browser = await chromium.launch();
  } catch (e) {
    // No browser is a missing tool, not a failed check: fx reports it as skipped.
    report.browser_missing = String(e && e.message || e).split('\n')[0];
    save();
    return;
  }
  try {
    for (const variant of variants) {
      try {
        await runIn(variant);
      } catch (e) {   // the window could not be set up: the script never ran this way
        const err = tag(variant) + String(e && e.stack || e).split('\n').slice(0, 8).join('\n');
        report.error = report.error ? report.error + '\n' + err : err;
      }
    }
  } finally {
    save();
    if (browser) await browser.close();
  }

  async function runIn(variant) {
    const isDesktop = variant === 'desktop';
    const isApp = variant === 'app';
    const viewports = isDesktop ? desktop.viewports : webViewports;
    const prefix = tag(variant);
    const filePrefix = prefix ? `${variant}-` : '';
    const D = desktop ? load_desktop() : null;
    let page = null;
    let session = null;
    let context;
    const t = {
      browser, base, variant: variant || 'web', desktop: null, ipc: null,
      url: (p) => (/^https?:|^file:/.test(p) ? p : t.base + (p.startsWith('/') ? p : '/' + p)),
    };
    const attachPage = (p) => {
      page = t.page = p;
      p.on('console', (m) => {
        if (m.type() === 'error') report.console.push(Object.assign({ text: m.text(), url: p.url() }, variant ? { variant } : {}));
      });
      p.on('pageerror', (e) => report.console.push(Object.assign({ text: String(e && e.message || e), url: p.url(), uncaught: true },
                                                                   variant ? { variant } : {})));
      p.on('requestfailed', (r) => {
        const f = r.failure();
        if (f && /ERR_ABORTED/.test(f.errorText)) return; // navigation away, not an error
        if (D && D.isIpcUrl(r.url())) return;              // Tauri's IPC: in report.ipc, not a request of the page
        report.requests.push(Object.assign({ url: r.url(), method: r.method(), failure: f ? f.errorText : 'failed' }, variant ? { variant } : {}));
      });
      p.on('response', (r) => {
        if (r.status() >= 400 && !(D && D.isIpcUrl(r.url()))) {
          report.requests.push(Object.assign({ url: r.url(), method: r.request().method(), status: r.status(),
                                               blocking: r.status() >= blockStatus }, variant ? { variant } : {}));
        }
      });
      if (isApp) D.recordIpc(p, (call) => t.ipc.log.add(call));
    };
    if (isDesktop || isApp) {
      t.desktop = Object.assign({ framework: desktop.framework || 'tauri', os: desktop.os || 'windows', real: isApp },
                                desktop.app || {});
    }
    if (isApp) {
      // the real app: started for this run, its WebView2 reached over CDP
      report.desktop.real = report.desktop.real || {};
      try {
        session = await D.launchApp(desktop.app_run || {}, chromium);
      } catch (e) {
        const why = String(e && e.message || e).split('\n')[0];
        // an app that ended by itself while starting is the code's (a failure of the check); a DevTools port that
        // never opened while it ran is this machine's (a note: the real app was not run)
        if (e && e.appExited) throw new Error('the app ended while starting: ' + why);
        report.desktop.real.launch_error = why;
        report.desktop.real.finished = true;
        return;
      }
      context = t.context = session.context;
      t.browser = session.browser;
      t.base = new URL(session.page.url()).origin;
      t.ipc = D.ipcOfApp(report, () => page);
      attachPage(session.page);
      await page.reload({ waitUntil: 'load' }).catch(() => {});   // what the app did before Playwright was there, again
      report.desktop.real = Object.assign(report.desktop.real || {}, { origin: t.base,
        window: await page.evaluate(() => [window.innerWidth, window.innerHeight]).catch(() => null) });
    } else {
      context = t.context = await browser.newContext({ viewport: { width: viewports[0][0], height: viewports[0][1] } });
      if (isDesktop) t.ipc = await D.attach(context, report, desktop, () => page, t);
      attachPage(await context.newPage());
    }
    // t.restart(): the app closed and opened again with its data kept; a page is loaded again
    t.restart = async () => {
      if (isApp) {
        attachPage(await session.restart());
        await page.waitForLoadState('load').catch(() => {});
      } else {
        await page.reload({ waitUntil: 'load' });
      }
    };

    const axe = axePath();
    let axeSource = null;
    Object.assign(t, {
      async goto(p, opts) {
        const res = await page.goto(t.url(p), Object.assign({ waitUntil: 'networkidle' }, opts || {}));
        if (res && !report.document) {
          report.document = { url: res.url(), status: res.status(), headers: await res.allHeaders() };
        }
        return res;
      },
      async snap(name, opts) {
        const n = filePrefix + slug(name);
        if (isApp) {   // the real window as it is: never resized
          const [w, h] = await page.evaluate(() => [window.innerWidth, window.innerHeight]);
          const file = path.join(shots, `${n}-${w}x${h}.png`);
          await page.screenshot({ path: file, fullPage: !(opts && opts.fullPage === false) });
          report.shots.push({ name: prefix + name, viewport: `${w}x${h}`, file, url: page.url(), variant });
        }
        for (const [w, h] of (isApp ? [] : viewports)) {
          await page.setViewportSize({ width: w, height: h });
          await page.waitForTimeout(150);
          const file = path.join(shots, `${n}-${w}x${h}.png`);
          await page.screenshot({ path: file, fullPage: !(opts && opts.fullPage === false) });
          report.shots.push(Object.assign({ name: prefix + name, viewport: `${w}x${h}`, file, url: page.url() }, variant ? { variant } : {}));
        }
        if (!isApp) await page.setViewportSize({ width: viewports[0][0], height: viewports[0][1] });
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
          for (const v of res) report.a11y.push(Object.assign({ page: prefix + name, url: page.url() }, v));
        } else if (!axe) {
          report.a11y_skipped = 'axe-core not installed (fx tools install playwright)';
        }
        save();
      },
      async step(name, body) {
        const s = { name: prefix + name, ok: true };
        if (variant) s.variant = variant;
        try { await body(); } catch (e) {
          s.ok = false; s.error = String(e && e.message || e).split('\n').slice(0, 6).join('\n');
          try {
            const file = path.join(shots, `failed-${filePrefix}${slug(name)}.png`);
            await page.screenshot({ path: file, fullPage: true });
            s.shot = file;
          } catch (e2) { /* page may be gone */ }
        }
        report.steps.push(s);
        save();
        return s.ok;
      },
      async design(code, opts) {
        // the real app draws what the browser run drew (both Chromium): measured there, once
        if (isApp) return { code: String(code), skipped: 'measured in the browser run' };
        const k = report.design.length + 1;
        const entry = { code: String(code), name: String(code), url: page.url(), level: null };
        if (variant) entry.variant = variant;
        report.design.push(entry);
        const pkg = (setup.packages || []).find((p) => (p.codes || []).includes(entry.code));
        if (setup.why || !pkg) {
          entry.notComparable = setup.why || `no design package of this run has the state ${entry.code}`;
          save();
          return entry;
        }
        const before = page.viewportSize();
        try {
          const dir = path.join(pkg.root, 'web', 'autotest', 'uicheck');
          if (!engines[pkg.root]) {
            engines[pkg.root] = { uiCheck: require(dir).uiCheck, annotate: require(path.join(dir, 'annotate')).annotate };
          }
          const engine = engines[pkg.root];
          if (setup.viewport) {
            await page.setViewportSize({ width: setup.viewport[0], height: setup.viewport[1] });
            await page.waitForTimeout(150);
          }
          entry.viewport = page.viewportSize();
          // the comparer hands its images and findings to the test's attachments: here, to this object
          const got = {};
          const ui = engine.uiCheck(browser, page, { attach: async (name, a) => { got[name] = a.body; } });
          const res = await ui.check('INITIAL', 1, entry.code, opts || {});
          await ui.finish();
          const checks = got.ui_checks ? JSON.parse(String(got.ui_checks)) : [];
          const states = got['ui-states'] ? JSON.parse(String(got['ui-states'])) : [];
          const info = got['ui-info'] ? JSON.parse(String(got['ui-info'])) : {};
          const c = checks[0];
          entry.level = res && res.level ? res.level : null;
          entry.package = info.package || pkg.version || null;
          if (!entry.level) entry.notComparable = (res && res.notComparable) || (states[0] && states[0].why) || 'not compared';
          if (c) {
            entry.name = String(c.name || code).replace(/^[^·]*·\s*/, '');
            entry.diffs = c.diffs || [];
            entry.diff_percent = c.diff_percent;
            entry.part = (info.parts || {})[c.screenshot] || null;
            const fbase = path.join(shots, `design-${k}-${filePrefix}${slug(code)}`);
            fs.writeFileSync(entry.web = `${fbase}-web.png`, got[c.screenshot]);
            fs.writeFileSync(entry.design = `${fbase}-design.png`, got[c.design_screenshot]);
            const bad = entry.diffs.filter((d) => d.level === 'BAD');
            if (bad.length) {
              fs.writeFileSync(entry.marked = `${fbase}-marked.png`,
                engine.annotate(got[c.screenshot], bad, { viewportWidth: entry.viewport ? entry.viewport.width : 0 }));
            }
          }
          if (info.limits && info.limits.length) entry.limits = info.limits;
          if (got['ui-bo-qua']) entry.left_out = String(got['ui-bo-qua']).trim().split('\n').slice(0, 20);
        } catch (e) {
          entry.level = null;
          entry.notComparable = String(e && e.message || e).split('\n')[0].slice(0, 300);
        } finally {
          if (setup.viewport && before) await page.setViewportSize(before).catch(() => {});
        }
        save();
        return entry;
      },
    });

    try {
      await fn(t);
    } catch (e) {
      const err = prefix + String(e && e.stack || e).split('\n').slice(0, 8).join('\n');
      report.error = report.error ? report.error + '\n' + err : err;
    } finally {
      // the script ran to its end on the real app: fx tells a run that was stopped (a timeout) by its absence
      if (isApp) report.desktop.real.finished = true;
      save();
      if (session) await session.stop();
      else await context.close().catch(() => {});
    }
  }
}

function load_desktop() {
  return require(path.join(__dirname, 'factory-desktop.js'));
}

// ---------------------------------------------------------------- a mobile app, on one virtual phone
async function runMobile(fn) {
  const shots = process.env.FACTORY_UI_SHOTS || path.resolve('ui-shots');
  const reportPath = process.env.FACTORY_UI_REPORT || path.resolve('ui-report.json');
  fs.mkdirSync(shots, { recursive: true });
  const report = { base: process.env.BASE_URL || '', steps: [], shots: [], console: [], requests: [], a11y: [], design: [], document: null, error: null };
  const save = () => fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  const setup = designSetup();
  const engines = {};
  const libPath = process.env.FACTORY_UI_APP_LIB;
  if (!libPath || !fs.existsSync(libPath)) {
    report.error = 'the auto test skill (its runtime app.js) is not installed next to this one';
    save();
    return;
  }
  const A = require(libPath);
  const AP = require(path.join(path.dirname(libPath), 'uicheck', 'app.js'));
  const device = A.device();
  report.device = { key: device.key, name: device.name, os: device.os, screen: device.screen };
  let app, browser;
  const crashes = async () => {
    for (const c of await A.crashes(app).catch(() => [])) report.console.push({ text: c.text, url: device.key });
  };
  try {
    app = await A.openApp(device);
  } catch (e) {
    report.error = 'the app could not be started on ' + device.key + ': ' + String(e && e.message || e).split('\n')[0];
    save();
    return;
  }
  const t = {
    app, device, page: null, base: report.base,
    goto: async () => { throw new Error('a mobile app has no address: use t.openLink(url) for a deep link, or tap through the screens'); },
    tap: (x, o) => A.tap(app, x, o), type: (l, v, o) => A.type(app, l, v, o), see: (x, o) => A.see(app, x, o),
    notSee: (x, o) => A.notSee(app, x, o), find: (x, o) => A.find(app, x, o), swipe: (d, s) => A.swipe(app, d, s),
    back: () => A.back(app), restart: () => A.restart(app), background: (s) => A.background(app, s),
    openLink: (u) => A.openLink(app, u), answerPermission: (allow, ms) => A.answerPermission(app, allow, ms),
    reset: () => A.reset(app),
    async snap(name, opts) {
      const file = path.join(shots, `${slug(name)}-${device.key}.png`);
      fs.writeFileSync(file, await A.shot(app));
      report.shots.push({ name, viewport: device.key, file, url: device.key });
      if (!(opts && opts.a11y === false)) {
        try {
          const c = await AP.capture(app, device);
          for (const v of AP.a11yOf(c.R0.nodes, c.platform)) report.a11y.push(Object.assign({ page: name, url: device.key }, v));
        } catch (e) { report.a11y_skipped = 'the screen could not be read: ' + String(e && e.message || e).split('\n')[0]; }
      }
      await crashes();
      save();
    },
    async step(name, body) {
      const s = { name, ok: true };
      try { await body(); } catch (e) {
        s.ok = false; s.error = String(e && e.message || e).split('\n').slice(0, 6).join('\n');
        try {
          const file = path.join(shots, `failed-${slug(name)}-${device.key}.png`);
          fs.writeFileSync(file, await A.shot(app));
          s.shot = file;
        } catch (e2) { /* the session may be gone */ }
      }
      await crashes();
      report.steps.push(s);
      save();
      return s.ok;
    },
    async design(code, opts) {
      const k = report.design.length + 1;
      const entry = { code: String(code), name: String(code), url: device.key, level: null };
      report.design.push(entry);
      const pkg = (setup.packages || []).find((p) => (p.codes || []).includes(entry.code));
      if (setup.why || !pkg) {
        entry.notComparable = setup.why || `no design package of this run has the state ${entry.code}`;
        save();
        return entry;
      }
      try {
        if (!browser) {
          try { browser = await load('playwright').chromium.launch(); } catch (e) {
            report.browser_missing = String(e && e.message || e).split('\n')[0];
            throw new Error('the design page needs Chromium (fx tools install playwright)');
          }
        }
        const dir = path.join(pkg.root, 'web', 'autotest', 'uicheck');
        if (!engines[pkg.root]) engines[pkg.root] = { uiCheck: require(dir).uiCheck, annotate: require(path.join(dir, 'annotate')).annotate };
        const engine = engines[pkg.root];
        const got = {};
        const ui = engine.uiCheck(browser, app, { attach: async (name, a) => { got[name] = a.body; } });
        const res = await ui.check('INITIAL', 1, entry.code, opts || {});
        await ui.finish();
        const checks = got.ui_checks ? JSON.parse(String(got.ui_checks)) : [];
        const states = got['ui-states'] ? JSON.parse(String(got['ui-states'])) : [];
        const info = got['ui-info'] ? JSON.parse(String(got['ui-info'])) : {};
        const c = checks[0];
        entry.level = res && res.level ? res.level : null;
        entry.viewport = info.viewport || null;
        entry.package = info.package || pkg.version || null;
        if (!entry.level) entry.notComparable = (res && res.notComparable) || (states[0] && states[0].why) || 'not compared';
        if (c) {
          entry.name = String(c.name || code).replace(/^[^·]*·\s*/, '');
          entry.diffs = c.diffs || [];
          entry.diff_percent = c.diff_percent;
          entry.part = (info.parts || {})[c.screenshot] || null;
          const base = path.join(shots, `design-${k}-${slug(code)}-${device.key}`);
          fs.writeFileSync(entry.web = `${base}-app.png`, got[c.screenshot]);
          fs.writeFileSync(entry.design = `${base}-design.png`, got[c.design_screenshot]);
          const bad = entry.diffs.filter((d) => d.level === 'BAD');
          if (bad.length) fs.writeFileSync(entry.marked = `${base}-marked.png`, engine.annotate(got[c.screenshot], bad, { viewportWidth: entry.viewport ? entry.viewport.width : 0 }));
        }
        if (info.limits && info.limits.length) entry.limits = info.limits;
        if (got['ui-bo-qua']) entry.left_out = String(got['ui-bo-qua']).trim().split('\n').slice(0, 20);
      } catch (e) {
        entry.level = null;
        entry.notComparable = String(e && e.message || e).split('\n')[0].slice(0, 300);
      }
      save();
      return entry;
    },
  };
  try {
    await fn(t);
  } catch (e) {
    report.error = String(e && e.stack || e).split('\n').slice(0, 8).join('\n');
  } finally {
    await crashes();
    save();
    await A.closeApp(app);
    if (browser) await browser.close().catch(() => {});
  }
}

module.exports = { run };
