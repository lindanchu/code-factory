// UI check of a functional case against the project's design package, one per case:
//   const { uiCheck } = require('../../autotest/uicheck');
//   const ui = uiCheck(browser, page, testInfo);
//   await step(C, 0, async () => { ...; await ui.check('INITIAL', 1, 'LG-1'); });
//   await step(C, 1, async () => { ...; await ui.check('AFTER_ACTION', 2, 'LG-2', { demo: { email } }); });
//   await step(C, 2, async () => { ...; await ui.check('FINAL', 3, 'PR-1'); });
//   await ui.finish();
// Each check: the page as it is now (what a user sees: the first screen, or only the open dialog / menu) is compared
// with the design page of that state code, opened from outside with the data the web shows at that moment. Both sides
// are measured by the same extractor, element by element. A screen far from the design (BAD) fails the current step,
// softly, when the project chose so. finish() attaches what the tool reads (tx): ui_checks, the images, ui-states,
// ui-bo-qua, ui-info.
//
// Read from the test repository (found by walking up from this folder to autotest.json):
//   autotest.json   conventions.ui_threshold.value     strict | medium | loose
//                   conventions.ui_percent_from.value  a number (px) | "off"
//                   conventions.ui_bad.value           step (a BAD screen fails the step) | column (the case passes)
//                   design.dir (default design-src), design.current (the version in use)
//   <design.dir>/<design.current>/autotest-states.json   the index of the package: pages, state codes, tokens
//   <web>/lib/ui-profile.js                             what only the project knows (below)
// Until the project's owner has chosen both ui_threshold and ui_percent_from, a check judges nothing: the state is
// recorded as not compared. Until ui_bad is chosen, a BAD screen does not fail its case.
//
// The profile, <web>/lib/ui-profile.js. Every hook is optional, except readData for states that need data; a hook may
// be async. `state` is { code, page, input, screen, label } of the index; `opts` is what the test gave to check();
// `scope` is page | dialog | menu.
//   anonymous: ['login']            design "screen" values that need no data
//   me({ page, state, code, opts, read })            -> who is logged in on the page (given on as `me`); default opts.me
//   readData({ browser, page, state, code, opts, me, scope, viewport, read })
//                                   -> the data the web shows, in the shape of the package's <Page>.data.json.
//                                      read = { view(page), rows(view, map, name), navJoin(view, map, list),
//                                      footer(view) }: the reader of view.js and the label map of tojson.js.
//                                      Called before and after the page is captured; contexts it opens are not filmed.
//   arrange({ data, state, code, opts, me, scope, webText, note })
//                                   -> the data with its objects moved to where the design takes them (a design draws
//                                      "the open project" as the first of the list). note(text) keeps a line in
//                                      ui-bo-qua when the data is changed for a reason of measuring.
//   sameObject({ data, state, code, opts, me, scope, webText })
//                                   -> null, or why the two sides would not show the same object. webText is
//                                      { text, head }: the text of the compared part with the values of its fields,
//                                      and the main heading of the page.
//   designInput({ state, data, opts, code, me })     -> the object given to the design page; default
//                                      { ...state.input, data }
//   region({ state, code, scope, viewport, opts })   -> null, or { keep: { x, y, w, h } } | { drop: { x, y, w, h } }
//                                      in px of the screen: the part compared. keep: a difference counts when its
//                                      element starts inside the kept part, on both sides. drop: a difference counts
//                                      when its element reaches outside the dropped part and its design element does
//                                      not start inside it (a frame shared by many screens is dropped everywhere but
//                                      in the feature that owns it).
//   partNote({ state, code, scope, region })         -> one sentence for the AI look: what part is compared
//   notWeb(diff, ctx)               -> null, or why this difference is not a difference of the web (a known limit of
//                                      the package, a measuring artefact). diff: { element, type, level, what, design,
//                                      actual, count }; ctx: { screen, step, code, state, scope, viewport, url, data,
//                                      opts, me, region, designNode, webNode, design, web, groups }.
//   packageLimits: ['...']          sentences for the AI look: what the package always draws differently
// The stamp of a result covers this folder, ui-profile.js and what it keeps next to it as ui-profile.<name>.js / .json (a
// label map in ui-profile.map.json), the thresholds in force and the package: `node index.js --stamp` prints the current
// one. What a profile takes from any other file is not covered: keep what decides a comparison in those files.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const extract = require('./extract');
const E = require('./engine');
const VIEW = require('./view');
const J = require('./tojson');
const { openDesign } = require('./design');
const { annotate } = require('./annotate');
const AP = require('./app');

const HERE = __dirname;
const SETTINGS = 'autotest.json';
const INDEX = 'autotest-states.json';
const PROFILE_DIR = path.join(HERE, '..', '..', 'lib');
const PROFILE = path.join(PROFILE_DIR, 'ui-profile.js');
const NO_MOTION = '*, *::before, *::after { transition: none !important; animation: none !important; caret-color: transparent !important; }';
const SCREEN_VI = { INITIAL: 'Ban đầu', AFTER_ACTION: 'Sau thao tác', FINAL: 'Màn cuối' };
const SCOPE_VI = { page: 'màn đầu', dialog: 'hộp thoại', menu: 'menu' };
const SCOPE = { dialog: '[role=dialog], [role=alertdialog], dialog[open]', menu: '[role=menu]' };
const ATTR = 'data-uicheck-scope';
const MARK = `[${ATTR}]`;
const RANK = { OK: 0, MINOR: 1, BAD: 2 };
const NOT_CHOSEN = 'chưa chọn ngưỡng lệch';
const NO_DATA_INPUT = 'gói design chưa nhận dữ liệu từ ngoài cho màn này';
const DESIGN_BROKEN = 'không so được: trang design không vẽ được màn này với dữ liệu đọc từ web';

// ---------------------------------------------------------------- the test repository
function findRoot(from = HERE) {
  for (let d = path.resolve(from); ; d = path.dirname(d)) {
    if (fs.existsSync(path.join(d, SETTINGS))) return d;
    if (path.dirname(d) === d) throw new Error(`uiCheck: không thấy ${SETTINGS} ở thư mục nào phía trên ${from}`);
  }
}
const json = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));

/** The thresholds in force, or null while the project's owner has not chosen them (both conventions). */
function thresholdsOf(cfg) {
  const conv = (cfg && cfg.conventions) || {};
  const val = (k) => (conv[k] && typeof conv[k] === 'object' ? conv[k].value : undefined);
  const preset = Object.prototype.hasOwnProperty.call(E.PRESETS, val('ui_threshold')) ? E.PRESETS[val('ui_threshold')] : null;
  const from = val('ui_percent_from');
  const percentFrom = from === 'off' ? null
    : typeof from === 'number' && Number.isFinite(from) && from >= 0 ? from
      : typeof from === 'string' && /^\d+(\.\d+)?$/.test(from.trim()) ? Number(from) : undefined;
  if (!preset || percentFrom === undefined) return null;
  return { px: preset.px, pct: preset.pct, fontPx: preset.fontPx, shade: preset.shade, radius: preset.radius, percentFrom };
}

function load(root = findRoot()) {
  const cfg = json(path.join(root, SETTINGS));
  const design = cfg.design || {};
  const pkg = design.current ? path.resolve(root, design.dir || 'design-src', String(design.current)) : null;
  const indexFile = pkg ? path.join(pkg, INDEX) : null;
  const index = indexFile && fs.existsSync(indexFile) ? json(indexFile) : null;
  const bad = ((cfg.conventions || {}).ui_bad || {}).value === 'step' ? 'step' : 'column';
  const version = String((index && index.version) || design.current || '').trim().replace(/\s+/g, '-');
  return { root, cfg, pkg, indexFile, index, version, thresholds: thresholdsOf(cfg), bad };
}

// ---------------------------------------------------------------- the stamp
function filesOf(dir, rel = '') {
  const out = [];
  for (const e of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
    if (e.name.startsWith('.') || e.name.endsWith('~')) continue;   // what a file browser or an editor leaves behind
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) out.push(...filesOf(dir, r)); else if (e.isFile()) out.push(r);
  }
  return out.sort();
}
/** The files of the profile: ui-profile.js and what it keeps next to it as ui-profile.<name>.js / .json (a label map). */
const profileFiles = () => (fs.existsSync(PROFILE_DIR) ? fs.readdirSync(PROFILE_DIR).filter(f => /^ui-profile(\.[A-Za-z0-9_-]+)*\.(js|json)$/.test(f) && fs.statSync(path.join(PROFILE_DIR, f)).isFile()).sort() : []);

/**
 * What judged a result: the comparer (every file of this folder), the profile, the thresholds in force and the design
 * package. It changes when one of them changes, and only then.
 */
function currentStamp(root) {
  const S = load(root);
  const h = crypto.createHash('sha256');
  const add = (label, content) => { h.update(`${label}\0`); h.update(content); h.update('\0'); };
  for (const f of filesOf(HERE)) add(`uicheck/${f}`, fs.readFileSync(path.join(HERE, f)));
  for (const f of profileFiles()) add(`profile/${f}`, fs.readFileSync(path.join(PROFILE_DIR, f)));
  add('thresholds', JSON.stringify(S.thresholds));
  add('package', String((S.index && S.index.sha256) || ''));
  return h.digest('hex');
}

// ---------------------------------------------------------------- set-up of a process (once)
let SETUP = null;
function setup() {
  if (SETUP) return SETUP;
  const S = load();
  if (!S.pkg) throw new Error(`uiCheck: ${SETTINGS} chưa ghi gói design đang dùng (design.current)`);
  if (!S.index) throw new Error(`uiCheck: gói design ${S.version} chưa có bảng trạng thái (${path.relative(S.root, S.indexFile)})`);
  const profile = fs.existsSync(PROFILE) ? require(PROFILE) : {};
  const tokens = S.index.tokens ? path.join(S.pkg, S.index.tokens) : null;
  E.setTokens(tokens && fs.existsSync(tokens) ? { file: tokens, prefix: S.index.token_prefix || undefined } : {});
  if (S.thresholds) E.setThresholds(S.thresholds);
  return (SETUP = { ...S, profile: profile || {}, stamp: currentStamp(S.root) });
}

// ---------------------------------------------------------------- in the page
/** Mark the visible dialog (else menu) of a page; a page may keep hidden dialogs in its markup. Returns the scope. */
const markScope = ([sels, attr]) => {
  document.querySelectorAll(`[${attr}]`).forEach(e => e.removeAttribute(attr));
  const vis = (e) => { const r = e.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return false; for (let a = e; a; a = a.parentElement) { const c = getComputedStyle(a); if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity === 0) return false; } return true; };
  for (const [k, sel] of sels) { const e = [...document.querySelectorAll(sel)].find(vis); if (e) { e.setAttribute(attr, k); return k; } }
  return 'page';
};
const unmark = (attr) => { document.querySelectorAll(`[${attr}]`).forEach(e => e.removeAttribute(attr)); };
const stillPage = ([css, attr]) => {
  if (document.querySelector(`style[${attr}]`)) return;
  const s = document.createElement('style'); s.setAttribute(attr, ''); s.textContent = css; document.head.appendChild(s);
};
/** The text of the compared part with the values of its fields, and the main heading of the page. */
const textOf = (sel) => {
  const root = (sel && document.querySelector(sel)) || document.querySelector('main') || document.body;
  const vals = [...root.querySelectorAll('input, textarea')].map(e => e.value || '').join(' ');
  const h1 = document.querySelector('main h1') || document.querySelector('h1');
  return { text: `${root.innerText} ${vals}`.replace(/\s+/g, ' '), head: h1 ? h1.innerText.replace(/\s+/g, ' ') : '' };
};
/** How the page is shown: the design page is opened the same way. */
const shownAs = () => ({ locale: navigator.language, timezoneId: Intl.DateTimeFormat().resolvedOptions().timeZone, deviceScaleFactor: window.devicePixelRatio || 1,
  colorScheme: window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light', width: window.innerWidth, height: window.innerHeight });
// two frames for the page to lay itself out again (or one second, on a page that draws no frame)
const frames = (p) => p.evaluate(() => new Promise(r => { const t = setTimeout(r, 1000); requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(t); r(); })); }));
const READ = { view: (p) => p.evaluate(VIEW), rows: J.rows, navJoin: J.navJoin, footer: J.footer };
// On an app, view() gives what a screen reader would: each text and field with its box (points / dp), top to bottom.
const viewOfApp = async (app) => {
  const c = await AP.capture(app, app.device);
  return { texts: c.R0.nodes.filter(n => n.type === 'text').map(n => ({ text: n.text, box: n.box })),
    fields: c.R0.nodes.filter(n => n.type === 'el' && n.role).map(n => ({ role: n.role, name: n.name, state: n.state || {}, box: n.box })) };
};
const READ_APP = { view: viewOfApp, rows: J.rows, navJoin: J.navJoin, footer: J.footer };

// ---------------------------------------------------------------- what is compared
/** Keep the nodes a user sees on the first screen (scope page), or the nodes of the dialog / menu; menu boxes relative to the menu. */
function cut(S, scope, vp) {
  const by = new Map(S.nodes.map(n => [n.id, n]));
  const sy = (S.scroll || {}).y || 0;
  const keep = new Set(S.nodes.filter(n => scope !== 'page' || (n.box.y < sy + vp.height && n.box.y + n.box.h > sy)).map(n => n.id));
  const up = (id) => { let p = by.get(id); while (p && !keep.has(p.id)) p = by.get(p.parent); return p ? p.id : 0; };
  let nodes = S.nodes.filter(n => keep.has(n.id)).map(n => ({ ...n, parent: up(n.parent) }));
  if (scope === 'menu' && nodes.length) {
    const ox = Math.min(...nodes.map(n => n.box.x)), oy = Math.min(...nodes.map(n => n.box.y));
    nodes = nodes.map(n => ({ ...n, box: { ...n.box, x: n.box.x - ox, y: n.box.y - oy } }));
    return { ...S, nodes, off: { x: ox, y: oy } };
  }
  return { ...S, nodes, off: { x: 0, y: 0 } };
}
/** The box of a node on the screen (its extract's own scroll and offset taken off), cut to the screen. */
function onScreen(b, S, vp) {
  const x = b.x + S.off.x - ((S.scroll || {}).x || 0), y = b.y + S.off.y - ((S.scroll || {}).y || 0);
  const x0 = Math.max(0, x), y0 = Math.max(0, y), x1 = Math.min(vp.width, x + b.w), y1 = Math.min(vp.height, y + b.h);
  return { x: x0, y: y0, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
}
const startsIn = (b, r) => b.x >= r.x && b.x < r.x + r.w && b.y >= r.y && b.y < r.y + r.h;
const within = (b, r) => b.x >= r.x && b.y >= r.y && b.x + b.w <= r.x + r.w && b.y + b.h <= r.y + r.h;
const isRect = (r) => !!r && ['x', 'y', 'w', 'h'].every(k => typeof r[k] === 'number' && Number.isFinite(r[k])) && r.w > 0 && r.h > 0;

/** Is this difference in the part the profile compares? (the rules of `region` at the top of this file) */
function inPart(d, region, Dn, Rn, dBy, vp) {
  const own = d.px ? onScreen(d.px, d.rNode !== undefined ? Rn : Dn, vp) : null;
  const dn = d.dNode !== undefined ? dBy.get(d.dNode) : null;
  const des = dn && dn.box ? onScreen(dn.box, Dn, vp) : null;
  if (region.keep) return (!own || startsIn(own, region.keep)) && (!des || startsIn(des, region.keep));
  return (!own || !within(own, region.drop)) && (!des || !startsIn(des, region.drop));
}

const oneLine = (t) => String(t).replace(/\s*[\r\n]+\s*/g, ' ');
const whatOf = (g) => g.what || (g.prop === 'missing' ? 'có trong design, không có trên web' : g.prop === 'extra' ? 'có trên web, không có trong design' : g.prop);
const unmatched = (g) => !g.what && (g.prop === 'missing' || g.prop === 'extra');
/** A difference not counted, as one line of ui-bo-qua. */
const lineOf = (g) => oneLine(`${g.level} ${g.element} · ${whatOf(g)}${unmatched(g) ? '' : `: ${g.design} → ${g.actual}`}`);

/** The differences in the shape the reporting web takes: box in percent of the screen, on the web's screenshot. */
function hubDiffs(kept, vp, Rn) {
  const pct = (v) => Math.round(Math.max(0, Math.min(100, v)) * 100) / 100;
  const sx = (Rn.scroll || {}).x || 0, sy = (Rn.scroll || {}).y || 0;
  return kept.map(g => {
    const b = g.px || {};
    const x = Math.max(0, Math.min((b.x || 0) + Rn.off.x - sx, vp.width - 1)), y = Math.max(0, Math.min((b.y || 0) + Rn.off.y - sy, vp.height - 1));
    const w = Math.min(Math.max(1, b.w || 1), vp.width - x), h = Math.min(Math.max(1, b.h || 1), vp.height - y);
    return { element: (g.element + (g.count > 1 ? ` (×${g.count})` : '')).slice(0, 120), type: g.type, level: g.level,
      design: (unmatched(g) ? String(g.design) : `${whatOf(g)}: ${g.design}`).slice(0, 255), actual: String(g.actual).slice(0, 255),
      box: { x: pct(100 * x / vp.width), y: pct(100 * y / vp.height), w: pct(100 * w / vp.width), h: pct(100 * h / vp.height) } };
  });
}

/** What part of the screen is compared, in one sentence (for the AI look), when the profile does not say. */
function partOf(scope, region, vp) {
  const at = (r) => `rộng ${Math.round(r.w)}, cao ${Math.round(r.h)} px tính từ điểm (${Math.round(r.x)}, ${Math.round(r.y)})`;
  const base = scope === 'dialog' ? 'hộp thoại đang mở' : scope === 'menu' ? 'menu đang mở' : `màn đầu (${vp.width}×${vp.height})`;
  const behind = scope === 'page' ? '' : '; phần màn phía sau không so';
  if (region && region.keep) return `Chỉ so vùng ${at(region.keep)} của ${base}; phần còn lại không so.`;
  if (region && region.drop) return `So ${base}, trừ vùng ${at(region.drop)}${behind}.`;
  return scope === 'page' ? `So cả ${base}.` : `Chỉ so ${base}${behind}.`;
}

/** A difference nobody can see, whatever the product: the hint of a field under the value typed in it. */
const cannotBeSeen = (g, designNode) => (g.prop === 'placeholder' && designNode && designNode.state && designNode.state.value
  ? 'Không nhìn thấy: ô đã có giá trị nên chữ gợi ý không hiện' : null);

// ---------------------------------------------------------------- one UI check per case
// The UI checks not finished yet, of every test of this worker process (test.js finishes them when a test ends).
const OPEN = (globalThis.__autotestUiOpen ||= new Set());
function uiCheck(browser, page, testInfo) {
  // `page` is a Playwright page on the web, or a WebdriverIO session of the app on a phone (an app: only the reading
  // of the screen differs, see app.js; the design side and the comparison are the same)
  const appMode = AP.isApp(page);
  const read = appMode ? READ_APP : READ;
  const checks = [], shots = {}, lines = [], states = [];
  let marked = null, viewport = null, done = false;
  // A case that fails before its own finish() still sends the screens it compared: test.js finishes every UI check of
  // the test that is still open when the test ends (OPEN, by testInfo).
  const ui = {
    /**
     * Compare the page now with design state `code` (e.g. 'US-5'). screen: INITIAL | AFTER_ACTION | FINAL; step: the
     * 1-based step of the case. opts: what the project's profile needs to know about this screen (the values the test
     * typed, the object the page is about); opts.me: who is logged in, when the profile does not read it itself.
     * Returns { level, diffs }, or { level: null, notComparable } when the screen could not be compared.
     */
    async check(screen, step, code, opts = {}) {
      if (!SCREEN_VI[screen]) throw new Error(`uiCheck: màn phải là INITIAL, AFTER_ACTION hoặc FINAL (đang là "${screen}")`);
      if (!Number.isInteger(step) || step < 1) throw new Error(`uiCheck: bước phải là số nguyên từ 1 (đang là "${step}")`);
      const C = setup();
      const P = C.profile;
      const def = (C.index.states || {})[code];
      if (!def) throw new Error(`uiCheck: gói design ${C.version} không có trạng thái ${code}`);
      const state = { code, page: def.page, input: def.input || {}, screen: def.screen || (def.input || {}).screen || '', label: def.label || '' };
      const notCompared = (reason, scope, more = {}) => {
        const why = oneLine(reason);
        lines.push(`[${screen}] ${code} — ${why}`);
        states.push({ screen, step, code, ...(scope ? { scope } : {}), level: null, sent: false, why });
        return { level: null, diffs: [], notComparable: why, ...more };
      };
      if (!C.thresholds) return notCompared(NOT_CHOSEN);
      if (((C.index.pages || {})[state.page] || {}).takes_data === false) return notCompared(NO_DATA_INPUT);

      // the page as a user sees it, at rest
      let shown, vp, cap = null;
      if (appMode) {
        cap = await AP.capture(page, page.device);
        shown = cap.shown;
        vp = cap.vp;
      } else {
        shown = await page.evaluate(shownAs);
        vp = page.viewportSize() || { width: shown.width, height: shown.height };
      }
      viewport = vp;
      if (!appMode) {
        await page.mouse.move(1, 1).catch(() => {});
        await page.evaluate(stillPage, [NO_MOTION, 'data-uicheck-still']).catch(() => {});
        await page.evaluate(() => (document.fonts ? document.fonts.ready.then(() => null) : null));
        await frames(page);
      }

      const anonymous = (P.anonymous || []).includes(state.screen);
      // an app is compared as a whole screen (its dialogs have no markup to cut them out by)
      let scope = appMode ? 'page' : await page.evaluate(markScope, [Object.entries(SCOPE), ATTR]);
      const note = (text) => { lines.push(`[${screen}] ${code} — ${oneLine(text)}`); };
      let me = opts.me !== undefined ? opts.me : null;
      if (!anonymous && opts.me === undefined && typeof P.me === 'function') me = await P.me({ page, app: appMode ? page : undefined, state, code, opts, read });
      const readData = async () => {
        if (typeof P.readData !== 'function') throw new Error(`uiCheck: trạng thái ${code} cần dữ liệu đọc từ web, nhưng lib/ui-profile.js chưa có readData (hoặc chưa ghi màn "${state.screen}" vào anonymous)`);
        const before = globalThis.__noVideo;
        globalThis.__noVideo = true;   // contexts opened to read data are not part of the case's video (test.js)
        let got;
        try { got = await P.readData({ browser, page, app: appMode ? page : undefined, state, code, opts, me, scope, viewport: vp, read }); } finally { globalThis.__noVideo = before; }
        // without data the design would draw its own sample: never a base to compare the web with
        if (got === null || typeof got !== 'object') throw new Error(`uiCheck: readData của lib/ui-profile.js không trả về dữ liệu cho trạng thái ${code}`);
        return got;
      };
      // The web page is captured between two reads of the data: other tests running at the same time change the data,
      // so when the two reads differ the page is captured again (at most 3 times).
      let data = anonymous ? null : await readData();
      let R0, shot, webText;
      for (let i = 0; i < 3; i++) {
        if (appMode) {
          if (i > 0) cap = await AP.capture(page, page.device);
          ({ R0, webText, shot } = cap);
        } else {
          scope = await page.evaluate(markScope, [Object.entries(SCOPE), ATTR]);
          R0 = await page.evaluate(extract, { rootSel: scope === 'page' ? null : MARK });
          webText = await page.evaluate(textOf, scope === 'page' ? null : MARK);
          shot = await page.screenshot();
        }
        if (anonymous) break;
        const again = await readData();
        const same = JSON.stringify(again) === JSON.stringify(data);
        data = again;
        if (same) break;
      }
      if (!appMode) await page.evaluate(unmark, ATTR).catch(() => {});

      if (!anonymous) {
        // the design takes objects by their position in the data
        if (typeof P.arrange === 'function') { const moved = await P.arrange({ data, state, code, opts, me, scope, webText, note }); if (moved !== undefined && moved !== null) data = moved; }
        const why = typeof P.sameObject === 'function' ? await P.sameObject({ data, state, code, opts, me, scope, webText }) : null;
        if (why) return notCompared(/^không so được/.test(String(why)) ? String(why) : `không so được: ${why}`, scope);
      }

      // the design page of the state, with the data, opened from outside: no network, same screen as the web page
      const input = typeof P.designInput === 'function' ? await P.designInput({ state, data, opts, code, me })
        : (data === null || data === undefined ? { ...state.input } : { ...state.input, data });
      const ref = await openDesign(browser, path.join(C.pkg, state.page), input, vp,
        { locale: shown.locale, timezoneId: shown.timezoneId, deviceScaleFactor: shown.deviceScaleFactor, colorScheme: shown.colorScheme });
      let D0, dshot, layer = scope !== 'page', broken = [];
      try {
        await ref.page.addStyleTag({ content: NO_MOTION }).catch(() => {});
        if (scope !== 'page') {
          layer = (await ref.page.evaluate(markScope, [[[scope, SCOPE[scope]]], ATTR])) === scope;
          if (layer) await ref.page.locator(MARK).first().scrollIntoViewIfNeeded().catch(() => {});
        }
        await frames(ref.page);
        // without the layer on the design side, the design is read as a whole screen
        D0 = await ref.page.evaluate(extract, { rootSel: layer ? MARK : null, skipRootPaint: !layer });
        dshot = await ref.page.screenshot();
        broken = ref.errors.slice();
      } finally { await ref.close(); }
      // a design page that stopped on an error while drawing this data is not a reference
      if (broken.length) return notCompared(DESIGN_BROKEN, scope, { designErrors: broken });

      let Dn, Rn, diffs;
      if (scope !== 'page' && !layer) {
        // the web shows a dialog / menu that this design state does not have: one difference, the layer itself
        Dn = cut(D0, 'page', vp);
        Rn = { ...R0, off: { x: 0, y: 0 } };
        const x0 = Math.min(...R0.nodes.map(n => n.box.x)), y0 = Math.min(...R0.nodes.map(n => n.box.y));
        const x1 = Math.max(...R0.nodes.map(n => n.box.x + n.box.w)), y1 = Math.max(...R0.nodes.map(n => n.box.y + n.box.h));
        diffs = [{ id: 'layer', element: scope === 'dialog' ? 'Hộp thoại đang mở' : 'Menu đang mở', prop: 'extra', type: 'MISSING_ELEMENT', level: 'BAD', design: 'không có', actual: 'có',
          px: R0.nodes.length ? { x: x0, y: y0, w: x1 - x0, h: y1 - y0 } : { x: 0, y: 0, w: vp.width, h: vp.height }, rNode: 0 }];
      } else {
        Dn = cut(D0, scope, vp);
        Rn = cut(R0, scope, vp);
        // a difference whose design element is itself broken by the data is not a base to judge on
        const match = E.matchNodes(Dn, Rn);
        if (appMode) AP.fillUnmeasured(Dn, Rn, match);   // what an app cannot tell is not a difference (app.js)
        diffs = E.compare(Dn, Rn, match, { focus: false }).diffs.filter(d => !d.refBroken);
        if (appMode) diffs = AP.dropUnseen(diffs, Dn, match);   // a box that only paints has no node on an app (app.js)
      }
      const dBy = new Map(Dn.nodes.map(n => [n.id, n])), rBy = new Map(Rn.nodes.map(n => [n.id, n]));

      // the part the profile compares
      const region = typeof P.region === 'function' ? (await P.region({ state, code, scope, viewport: vp, opts })) || null : null;
      if (region && !isRect(region.keep) && !isRect(region.drop)) throw new Error('uiCheck: region của lib/ui-profile.js phải trả về { keep: { x, y, w, h } } hoặc { drop: { x, y, w, h } }, tính bằng px');
      if (region) diffs = diffs.filter(d => inPart(d, region, Dn, Rn, dBy, vp));

      // one finding per cause; what is not a difference of the web is kept aside, with its reason
      const groups = E.group(diffs);
      const ctx = { screen, step, code, state, scope, viewport: vp, url: appMode ? cap.url : page.url(), data, opts, me, region, design: Dn, web: Rn, groups, app: appMode };
      const kept = [];
      for (const g of groups) {
        const designNode = g.dNode !== undefined ? dBy.get(g.dNode) || null : null, webNode = g.rNode !== undefined ? rBy.get(g.rNode) || null : null;
        let why = cannotBeSeen(g, designNode);
        if (!why && typeof P.notWeb === 'function') why = await P.notWeb(g, { ...ctx, designNode, webNode });
        if (why) lines.push(`[${screen}] ${code} ${lineOf(g)} — ${oneLine(why)}`); else kept.push(g);
      }

      const level = E.levelOf(kept);
      let hd = hubDiffs(kept, vp, Rn);
      if (hd.length > 50) hd = hd.sort((a, b) => (a.level === 'BAD' ? 0 : 1) - (b.level === 'BAD' ? 0 : 1)).slice(0, 50);
      const area = Math.min(100, hd.reduce((a, d) => a + d.box.w * d.box.h / 100, 0));
      const k = checks.length + 1;
      shots[`ui-${k}`] = shot;
      shots[`design-${k}`] = dshot;
      const said = typeof P.partNote === 'function' ? await P.partNote({ state, code, scope, region }) : null;
      const check = { name: `${SCREEN_VI[screen]} · ${code} ${state.label}`.trim().slice(0, 120), step, screenshot: `ui-${k}`, design_screenshot: `design-${k}`, design_source: 'REFERENCE',
        level, diff_percent: Math.round(area * 100) / 100, diffs: hd };
      checks.push({ check, part: said ? String(said) : partOf(scope, region, vp) });
      states.push({ screen, step, code, scope, level, check });
      const bad = hd.filter(d => d.level === 'BAD');
      if (bad.length && C.bad === 'step') {
        if (!marked) marked = annotate(shot, bad, { viewportWidth: vp.width });
        const { expect } = require('@playwright/test');
        // one line: the tool reads the first line of an error
        const what = oneLine(bad.map(d => `${d.element}: design ${d.design}, thực tế ${d.actual}`).join(' | ')).slice(0, 1500);
        expect.soft(level, `Giao diện lệch nặng so với design ${C.version} (${code}, ${SCOPE_VI[scope]}): ${what}`).not.toBe('BAD');
      }
      return { level, diffs: hd };
    },

    /** Attach the screens (at most one per step: the worst, the later one on a tie), their design images and what the tool reads. */
    async finish() {
      OPEN.delete(ui);
      if (done || !states.length) return;
      done = true;
      const C = setup();
      if (lines.length) await testInfo.attach('ui-bo-qua', { body: Buffer.from(lines.join('\n') + '\n'), contentType: 'text/plain' });
      const byStep = new Map();
      for (const c of checks) { const o = byStep.get(c.check.step); if (!o || RANK[c.check.level] >= RANK[o.check.level]) byStep.set(c.check.step, c); }
      const keep = [...byStep.values()].sort((a, b) => a.check.step - b.check.step);
      const parts = {};
      for (const c of keep) {
        for (const n of [c.check.screenshot, c.check.design_screenshot]) await testInfo.attach(n, { body: shots[n], contentType: 'image/png' });
        parts[c.check.screenshot] = c.part;
      }
      if (marked) await testInfo.attach('vung-lech', { body: marked, contentType: 'image/png' });
      if (keep.length) await testInfo.attach('ui_checks', { body: Buffer.from(JSON.stringify(keep.map(c => c.check))), contentType: 'application/json' });
      const sent = new Set(keep.map(c => c.check));
      await testInfo.attach('ui-states', { body: Buffer.from(JSON.stringify(states.map(({ check, ...s }) => (check ? { ...s, sent: sent.has(check) } : s)))), contentType: 'application/json' });
      const vp = viewport || (appMode ? null : page.viewportSize()) || { width: 0, height: 0 };
      const limits = (C.profile.packageLimits || []).map(String).concat(appMode ? AP.LIMITS : []);
      await testInfo.attach('ui-info', { body: Buffer.from(JSON.stringify({ package: C.version, package_sha: String(C.index.sha256 || ''), viewport: { width: vp.width, height: vp.height }, stamp: C.stamp,
        limits, parts, ...(appMode ? { device: (page.device && page.device.key) || null } : {}) })), contentType: 'application/json' });
    },
  };
  ui.testInfo = testInfo;
  OPEN.add(ui);
  return ui;
}

module.exports = { uiCheck, currentStamp, load };

if (require.main === module) {
  if (process.argv.includes('--stamp')) {
    try { process.stdout.write(currentStamp() + '\n'); } catch (e) { process.stderr.write(String((e && e.message) || e) + '\n'); process.exit(1); }
  } else {
    process.stderr.write('usage: node index.js --stamp\n');
    process.exit(2);
  }
}
