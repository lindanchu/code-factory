// UI check engine: matches what the real page shows with what the design reference shows (same data, same browser,
// same viewport), node by node, and classes every difference by the thresholds chosen for the project.
//   setThresholds(t)        the thresholds in force (a preset of PRESETS, chosen by the project's owner, never here)
//   setTokens({ file })     the tokens of the design package, to name a colour and to know its role
//   matchNodes(D, R)        pairs the nodes of the two extracts (extract.js)
//   compare(D, R, m, opts)  -> differences with a level (MINOR | BAD), grouped by root cause
// Positions are compared as spacing, not as coordinates: the gap to the neighbour before (above / left) inside the same
// parent, or the gap to the parent's edge. A taller element is one difference, not one per element below it.
const fs = require('fs');

// ---------- thresholds ----------
//   px           size / spacing / position difference above which it is BAD (0–1px is OK, 2px up to px is MINOR)
//   pct          share of the design value above which it is BAD
//   percentFrom  the design spacing (px) from which the share is applied; null: the share is never applied, to anything
//   fontPx       font size difference that is BAD
//   shade        level of another shade of the same role: MINOR | BAD (another role is always BAD)
//   radius       corner radius difference above which it is BAD
const PRESETS = Object.freeze({
  strict: Object.freeze({ px: 4, pct: 0.15, fontPx: 1, shade: 'BAD', radius: 2 }),
  medium: Object.freeze({ px: 8, pct: 0.25, fontPx: 2, shade: 'MINOR', radius: 4 }),
  loose: Object.freeze({ px: 16, pct: 0.40, fontPx: 4, shade: 'MINOR', radius: 8 }),
});
let T = null;
function setThresholds(t) {
  const num = (k) => typeof (t || {})[k] === 'number' && Number.isFinite(t[k]) && t[k] >= 0;
  if (!t || !['px', 'pct', 'fontPx', 'radius'].every(num) || !['MINOR', 'BAD'].includes(t.shade) || !(t.percentFrom === null || num('percentFrom')))
    throw new Error('uicheck engine: setThresholds needs { px, pct, fontPx, shade: MINOR|BAD, radius, percentFrom: number|null }');
  T = { px: t.px, pct: t.pct, fontPx: t.fontPx, shade: t.shade, radius: t.radius, percentFrom: t.percentFrom };
}
const thresholds = () => (T ? { ...T } : null);
const needThresholds = () => { if (!T) throw new Error('uicheck engine: no thresholds in force (setThresholds first)'); };

// ---------- tokens of the design package ----------
const parseColor = (v) => {
  v = String(v).trim();
  let m = v.match(/^#([0-9a-f]{3})$/i);
  if (m) return [...m[1]].map(c => parseInt(c + c, 16)).concat([1]);
  m = v.match(/^#([0-9a-f]{6})$/i);
  if (m) return [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16)).concat([1]);
  m = v.match(/^rgba?\(([^)]+)\)$/i);
  if (m) { const n = m[1].split(/[\s,/]+/).filter(Boolean).map(Number); return [n[0], n[1], n[2], n.length > 3 ? n[3] : 1]; }
  return null;
};
// The role of a token is read from its name without the package's prefix ("--ds-text-primary" -> "text-primary").
const groupOfToken = (rest) => {
  if (/^text-/.test(rest)) return 'text';
  // a dark surface (sidebar, primary button, toast) and a light one are different roles
  if (/^bg-dark/.test(rest)) return 'surface-dark';
  if (/^(bg-|white$)/.test(rest)) return 'surface';
  if (/^border/.test(rest)) return 'border';
  for (const g of ['success', 'warning', 'danger', 'info', 'skip', 'primary', 'ink']) if (rest.includes(g)) return g;
  return 'other';
};
/** The prefix a token file uses ("--ds-"): the first segment most of its names share, "--" when they share none. */
function prefixOf(names) {
  const count = new Map();
  for (const n of names) { const m = n.match(/^--[A-Za-z0-9_]+-/); if (m) count.set(m[0], (count.get(m[0]) || 0) + 1); }
  let best = '--', top = 0;
  for (const [p, c] of count) if (c > top) { best = p; top = c; }
  return top * 2 > names.length ? best : '--';
}
/**
 * The tokens of a package: { file | css, prefix, overrides }. prefix: as the package's index names it, else read from
 * the file. overrides: token values decided to differ from the package (name -> value).
 */
function loadTokens({ file, css, prefix, overrides } = {}) {
  const text = (css !== undefined ? String(css) : file ? fs.readFileSync(file, 'utf8') : '').replace(/\/\*[\s\S]*?\*\//g, '');
  const raw = {};
  for (const [, k, v] of text.matchAll(/(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+)(?=[;}])/g)) raw[k] = v.trim();
  Object.assign(raw, overrides || {});
  const pre = prefix || prefixOf(Object.keys(raw));
  const rest = (name) => (name.startsWith(pre) ? name.slice(pre.length) : name.replace(/^--/, ''));
  const resolve = (v, d = 0) => (d > 8 ? v : v.replace(/var\(\s*(--[A-Za-z0-9_-]+)\s*\)/g, (m0, k) => (raw[k] ? resolve(raw[k], d + 1) : m0)));
  const colors = [];   // { c: [r,g,b,a], names: [...], groups: Set } (base tokens first)
  const px = {};       // "12" -> [names]
  for (const [k, v0] of Object.entries(raw)) {
    const base = !/var\(/.test(v0);
    const v = resolve(v0);
    const c = parseColor(v);
    if (c) {
      let e = colors.find(x => x.c.join() === c.join());
      if (!e) { e = { c, names: [], groups: new Set() }; colors.push(e); }
      e.names.push(k);
      if (base) e.groups.add(groupOfToken(rest(k)));
    } else if (/^-?[\d.]+px$/.test(v)) (px[String(parseFloat(v))] ||= []).push(k);
  }
  return { raw, colors, px, prefix: pre, rest };
}
let TOK = loadTokens();   // no package yet: colours are compared by distance and named by their value only
const setTokens = (o) => { TOK = loadTokens(o); return { prefix: TOK.prefix, count: Object.keys(TOK.raw).length }; };
const dist = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) + Math.abs((a[3] ?? 1) - (b[3] ?? 1)) * 255;
/** Token names and role groups of a colour: exact value, else the nearest token colour (within 48 of 765). */
function tokenOf(c) {
  if (!c) return { names: [], groups: new Set(), exact: false };
  let best = null, bd = 1e9;
  for (const e of TOK.colors) { const d = dist(e.c, c); if (d < bd) { bd = d; best = e; } }
  if (best && bd <= 1) return { names: best.names, groups: best.groups, exact: true };
  if (best && bd <= 48) return { names: best.names, groups: best.groups, exact: false };
  return { names: [], groups: new Set(), exact: false };
}
const hex = (c) => (!c ? '—' : c[3] !== undefined && c[3] < 0.02 ? 'trong suốt' : '#' + c.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, '0')).join('').toUpperCase() + (c[3] !== undefined && c[3] < 0.99 ? ` ${Math.round(c[3] * 100)}%` : ''));
/** "#8A8F99 (--ds-text-placeholder)": the token is named for the property it is used on. */
function colorText(c, usage) {
  const t = tokenOf(c);
  if (!c || !t.exact) return hex(c) + (c && c[3] > 0.02 && TOK.colors.length ? ' (ngoài token)' : '');
  const pref = usage === 'text' ? /^text-/ : usage === 'bg' ? /^(bg-|white)/ : usage === 'border' ? /^border/ : null;
  const base = t.names.filter(n => !/var\(/.test(TOK.raw[n]));
  const pick = (pref && base.find(n => pref.test(TOK.rest(n)))) || base[0] || t.names[0];
  return `${hex(c)} (${pick}${base.length > 1 ? ' …' : ''})`;
}
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const blend = (top, under) => { const a = top[3] ?? 1; return [0, 1, 2].map(i => top[i] * a + under[i] * (1 - a)); };
const contrast = (fg, bg) => { const a = lum(blend(fg, bg)), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };

// ---------- geometry helpers ----------
const R_ = (b) => b.x + b.w, B_ = (b) => b.y + b.h, CX = (b) => b.x + b.w / 2, CY = (b) => b.y + b.h / 2;
const overlap = (a0, a1, b0, b1) => Math.min(a1, b1) - Math.max(a0, b0);
const index = (nodes) => { const m = new Map(); for (const n of nodes) m.set(n.id, n); return m; };
// What a control looks like, not what it is called: a link, a button, a tab and a menu item are all "something to press"
// (the prototype navigates with buttons where the product has links).
const kindOf = (n) => (n.type === 'text' ? 'text' : n.graphic ? 'g:' + n.graphic.kind : n.role ? 'c:' + (/^(textbox|combobox|searchbox)$/.test(n.role) ? 'field' : /^(checkbox|radio|switch)$/.test(n.role) ? 'check' : 'press') : 'box');

// ---------- matching ----------
function matchNodes(D, R) {
  const dBy = index(D.nodes), rBy = index(R.nodes);
  const m = new Map(), rev = new Map(), how = new Map();
  const link = (d, r, h) => { m.set(d.id, r.id); rev.set(r.id, d.id); how.set(d.id, h); };
  const free = (n, side) => !(side === 'd' ? m.has(n.id) : rev.has(n.id));
  const pairUp = (ds, rs, cost, limit, h) => {
    // greedy on ascending cost; document order breaks ties
    const c = [];
    ds.forEach((d, i) => rs.forEach((r, j) => { const v = cost(d, r) + Math.abs(i - j) * 0.01; if (v <= limit) c.push([v, d, r]); }));
    c.sort((a, b) => a[0] - b[0]);
    const ud = new Set(), ur = new Set();
    for (const [, d, r] of c) { if (ud.has(d.id) || ur.has(r.id)) continue; ud.add(d.id); ur.add(r.id); link(d, r, h); }
  };
  const place = (d, r) => Math.abs(CX(d.box) - CX(r.box)) + Math.abs(CY(d.box) - CY(r.box));

  // 1. text units with the same rendered text
  const squash = (t) => t.replace(/\s+/g, '');
  const group = (nodes) => { const g = new Map(); for (const n of nodes) if (n.type === 'text') { const k = squash(n.text); if (!g.has(k)) g.set(k, []); g.get(k).push(n); } return g; };
  const gd = group(D.nodes), gr = group(R.nodes);
  for (const [text, ds] of gd) {
    const rs = gr.get(text);
    if (!rs) continue;
    if (ds.length === rs.length) ds.forEach((d, i) => link(d, rs[i], 'text'));
    else pairUp(ds, rs, place, 1e9, 'text');
  }

  // 2. elements by the set of matched texts inside them
  const sigs = (nodes, by, key) => {
    const s = new Map();
    for (const n of nodes) { const k = n.type === 'text' ? key(n) : null; if (k == null) continue; for (let p = by.get(n.parent); p; p = by.get(p.parent)) { if (!s.has(p.id)) s.set(p.id, new Set()); s.get(p.id).add(k); } }
    return s;
  };
  const sd = sigs(D.nodes, dBy, n => (m.has(n.id) ? n.id : null)), sr = sigs(R.nodes, rBy, n => (rev.has(n.id) ? rev.get(n.id) : null));
  const sigKey = (set) => [...set].sort((a, b) => a - b).join(',');
  const elCost = (d, r) => (kindOf(d) === kindOf(r) ? 0 : (d.role || r.role || d.graphic || r.graphic) ? 60 : 0) + (!!d.paint === !!r.paint ? 0 : 6) + Math.abs(d.box.w - r.box.w) + Math.abs(d.box.h - r.box.h);
  const bySig = (nodes, s) => { const g = new Map(); for (const n of nodes) if (n.type === 'el' && s.has(n.id)) { const k = sigKey(s.get(n.id)); if (!g.has(k)) g.set(k, []); g.get(k).push(n); } return g; };
  const bd = bySig(D.nodes, sd), br = bySig(R.nodes, sr);
  for (const [k, ds] of bd) { const rs = br.get(k); if (rs) pairUp(ds, rs, elCost, 1e9, 'content'); }
  // close sets (one text missing or extra inside)
  {
    const ds = D.nodes.filter(n => n.type === 'el' && sd.has(n.id) && free(n, 'd')), rs = R.nodes.filter(n => n.type === 'el' && sr.has(n.id) && free(n, 'r'));
    const c = [];
    for (const d of ds) for (const r of rs) { const a = sd.get(d.id), b = sr.get(r.id); let inter = 0; for (const x of a) if (b.has(x)) inter++; const j = inter / (a.size + b.size - inter); if (j >= 0.5) c.push([-j, elCost(d, r), d, r]); }
    c.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
    for (const [, , d, r] of c) if (free(d, 'd') && free(r, 'r')) link(d, r, 'content~');
  }

  // 3. controls without matched text: by role and name
  {
    const key = (n) => kindOf(n) + '|' + (n.name || '');
    const g = (nodes, side) => { const o = new Map(); for (const n of nodes) if (n.type === 'el' && n.role && n.name && free(n, side)) { const k = key(n); if (!o.has(k)) o.set(k, []); o.get(k).push(n); } return o; };
    const cd = g(D.nodes, 'd'), cr = g(R.nodes, 'r');
    for (const [k, ds] of cd) { const rs = cr.get(k); if (rs) pairUp(ds, rs, place, 1e9, 'name'); }
  }

  // 4. the rest: same kind, nearest place inside the same matched parent (icons, bars, dividers, fields, texts that differ)
  const anc = (n, by, has) => { for (let p = by.get(n.parent); p; p = by.get(p.parent)) if (has(p.id)) return p; return null; };
  for (let round = 0; round < 6; round++) {
    const before = m.size;
    const gD = new Map(), gR = new Map();
    for (const d of D.nodes) if (free(d, 'd')) { const a = anc(d, dBy, id => m.has(id)); const k = (a ? a.id : 0) + '|' + kindOf(d); if (!gD.has(k)) gD.set(k, []); gD.get(k).push([d, a]); }
    for (const r of R.nodes) if (free(r, 'r')) { const a = anc(r, rBy, id => rev.has(id)); const k = (a ? rev.get(a.id) : 0) + '|' + kindOf(r); if (!gR.has(k)) gR.set(k, []); gR.get(k).push([r, a]); }
    for (const [k, dl] of gD) {
      const rl = gR.get(k);
      if (!rl) continue;
      const rel = (n, a) => ({ x: n.box.x - (a ? a.box.x : 0), y: n.box.y - (a ? a.box.y : 0), w: n.box.w, h: n.box.h });
      const A = new Map(dl.map(([n, a]) => [n.id, rel(n, a)])), Bm = new Map(rl.map(([n, a]) => [n.id, rel(n, a)]));
      const cost = (d, r) => { const p = A.get(d.id), q = Bm.get(r.id); return Math.abs(p.x - q.x) + Math.abs(p.y - q.y) + 0.5 * (Math.abs(p.w - q.w) + Math.abs(p.h - q.h)); };
      const ds = dl.map(x => x[0]), rs = rl.map(x => x[0]);
      // the same count: pair in reading order when every pair is plausible; else nearest first, within a limit
      const limit = (n) => Math.max(48, 0.6 * Math.max(n.box.w, n.box.h));
      if (ds.length === rs.length) {
        const ro = (l) => l.slice().sort((a, b) => (Math.abs(a.box.y - b.box.y) > 4 ? a.box.y - b.box.y : a.box.x - b.box.x));
        const od = ro(ds), or = ro(rs);
        if (od.every((d, i) => cost(d, or[i]) <= 2 * limit(d))) { od.forEach((d, i) => link(d, or[i], 'place')); continue; }
      }
      pairUp(ds, rs, cost, Math.max(...ds.map(limit)), 'place');
    }
    if (m.size === before) break;
  }
  // 5. what is left: same kind at nearly the same place on the page, whatever the nesting (an icon laid over a field in
  //    the design, inside the field's wrapper in the product)
  {
    const ds = D.nodes.filter(n => free(n, 'd')), rs = R.nodes.filter(n => free(n, 'r'));
    const c = [];
    for (const d of ds) for (const r of rs) {
      if (kindOf(d) !== kindOf(r)) continue;
      const v = Math.abs(CX(d.box) - CX(r.box)) + Math.abs(CY(d.box) - CY(r.box)) + 0.5 * (Math.abs(d.box.w - r.box.w) + Math.abs(d.box.h - r.box.h));
      if (v <= 16) c.push([v, d, r]);
    }
    c.sort((a, b) => a[0] - b[0]);
    for (const [, d, r] of c) if (free(d, 'd') && free(r, 'r')) link(d, r, 'near');
  }
  return { m, rev, how };
}

// ---------- levels ----------
const LV = { OK: 0, MINOR: 1, BAD: 2 };
const worst = (a, b) => (LV[a] >= LV[b] ? a : b);
/**
 * Size, spacing, position: off by 0–1px OK; 2px up to the threshold, and at most the threshold's share of the design
 * value, MINOR; else BAD. base: the design value the share is taken of (0: no share).
 */
function lvlPx(delta, base) {
  needThresholds();
  const d = Math.round(Math.abs(delta));
  if (d <= 1) return 'OK';
  if (d > T.px) return 'BAD';
  // the share is taken on the measured value, not on the rounded one (1.9px of 7.9px is 24 %, not 25.3 %)
  if (T.percentFrom !== null && base && base > 0 && Math.abs(delta) / base > T.pct) return 'BAD';
  return 'MINOR';
}
/** The design spacing the share is taken of: none under the value the project chose (a small gap doubles easily). */
const shareBase = (design) => (T.percentFrom !== null && Math.abs(design) >= T.percentFrom ? Math.abs(design) : 0);
/**
 * Another shade of the same role has the level the project chose (MINOR or BAD), another role is BAD. Many tokens share
 * one value (one colour may be the ink, the primary text and the dark surface), so the role is read for the property
 * the colour is used on: a background is never a "text" role, a text colour never a "surface" role.
 */
function lvlColor(dc, rc, usage) {
  needThresholds();
  const fit = (t) => new Set([...t.groups].filter(g => (usage === 'bg' ? g !== 'text' : usage === 'text' ? !/^surface|^border/.test(g) : true)));
  const a = fit(tokenOf(dc)), b = fit(tokenOf(rc));
  if (!a.size || !b.size) return dist(dc || [0, 0, 0, 0], rc || [0, 0, 0, 0]) > 120 ? 'BAD' : 'MINOR';
  for (const g of a) if (b.has(g)) return T.shade;
  return 'BAD';
}

const ROLE_VI = { button: 'Nút', link: 'Link', textbox: 'Ô', combobox: 'Ô chọn', checkbox: 'Ô tích', radio: 'Ô chọn một', tab: 'Tab', dialog: 'Hộp thoại', alert: 'Thông báo', status: 'Thông báo', tablist: 'Dãy tab', menu: 'Menu', menuitem: 'Mục menu', heading: 'Tiêu đề', navigation: 'Điều hướng', switch: 'Công tắc', img: 'Ảnh', option: 'Lựa chọn', listbox: 'Danh sách', row: 'Dòng', cell: 'Ô bảng', columnheader: 'Đầu cột', table: 'Bảng', search: 'Ô tìm', group: 'Nhóm', list: 'Danh sách', listitem: 'Mục', separator: 'Vạch ngăn', tabpanel: 'Nội dung tab', presentation: 'Khung', form: 'Form', main: 'Nội dung', complementary: 'Cột phụ', banner: 'Đầu trang', region: 'Vùng' };

// ---------- comparing ----------
function compare(D, R, match, opts = {}) {
  needThresholds();
  const { m, rev, how } = match;
  const dBy = index(D.nodes), rBy = index(R.nodes);
  const firstText = (S, by) => { const o = new Map(); for (const n of S.nodes) if (n.type === 'text') for (let p = by.get(n.parent); p; p = by.get(p.parent)) if (!o.has(p.id)) o.set(p.id, n.text); return o; };
  const ftD = firstText(D, dBy), ftR = firstText(R, rBy);
  const short = (s, n = 42) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const label = (n, ft) => {
    if (n.type === 'text') return `Chữ "${short(n.text)}"`;
    if (n.graphic) return n.graphic.kind === 'svg' ? `Icon${n.graphic.name ? ' ' + n.graphic.name : ''}${n.role ? ` (${ROLE_VI[n.role] || n.role} "${short(n.name || '', 30)}")` : ''}` : n.graphic.kind === 'canvas' ? 'Biểu đồ (canvas)' : n.graphic.kind === 'img' ? 'Ảnh' : 'Video';
    if (n.role) return `${ROLE_VI[n.role] || n.role} "${short(n.name || ft.get(n.id) || '')}"`;
    return ft.get(n.id) ? `Khung quanh "${short(ft.get(n.id))}"` : `Khung ${Math.round(n.box.w)}×${Math.round(n.box.h)}`;
  };
  const rootD = { id: 0, box: { x: 0, y: 0, w: D.doc.clientW || D.doc.w, h: D.doc.h } }, rootR = { id: 0, box: { x: 0, y: 0, w: R.doc.clientW || R.doc.w, h: R.doc.h } };
  const chain = (n, by) => { const c = []; for (let p = by.get(n.parent); p; p = by.get(p.parent)) c.push(p.id); return c; };
  // the nearest design ancestor whose counterpart is an ancestor of the real node
  const parentPair = (d, r) => {
    const rc = new Set(chain(r, rBy));
    for (let p = dBy.get(d.parent); p; p = dBy.get(p.parent)) if (m.has(p.id) && rc.has(m.get(p.id))) return [p, rBy.get(m.get(p.id))];
    return [rootD, rootR];
  };
  const pairs = [];
  for (const d of D.nodes) if (m.has(d.id)) { const r = rBy.get(m.get(d.id)); const [pd, pr] = parentPair(d, r); pairs.push({ d, r, pd, pr, diffs: [] }); }
  const byParent = new Map();
  for (const p of pairs) { if (!byParent.has(p.pd.id)) byParent.set(p.pd.id, []); byParent.get(p.pd.id).push(p); }
  const pairOf = new Map(pairs.map(p => [p.d.id, p]));

  const add = (p, prop, level, design, real, extra = {}) => { if (level !== 'OK') p.diffs.push({ prop, level, design, real, ...extra }); };
  for (const p of pairs) {
    const { d, r, pd, pr } = p;
    const sibs = byParent.get(pd.id).filter(q => q !== p);
    const textDiffers = d.type === 'text' && d.text.replace(/\s+/g, '') !== r.text.replace(/\s+/g, '');
    const fontSame = d.type === 'text' && d.font.size === r.font.size && String(d.font.weight) === String(r.font.weight) && d.font.family === r.font.family;

    // ---- spacing along each axis (not for the page background or the frame: their size is the content's)
    const pageLevel = d.page || r.page;
    for (const ax of pageLevel ? [] : ['x', 'y']) {
      const S = ax === 'x' ? (b) => b.x : (b) => b.y, E = ax === 'x' ? R_ : B_, C = ax === 'x' ? CX : CY;
      const O0 = ax === 'x' ? (b) => b.y : (b) => b.x, O1 = ax === 'x' ? B_ : R_;
      // neighbour before, in the design: ends before this one starts and shares the other axis
      let prev = null, hasNext = false;
      for (const q of sibs) {
        if (overlap(O0(q.d.box), O1(q.d.box), O0(d.box), O1(d.box)) <= 0.5) continue;
        if (E(q.d.box) <= S(d.box) + 1) { if (!prev || E(q.d.box) > E(prev.d.box)) prev = q; }
        else if (S(q.d.box) >= E(d.box) - 1) hasNext = true;
      }
      const dStart = S(d.box) - S(pd.box), rStart = S(r.box) - S(pr.box), dEnd = E(pd.box) - E(d.box), rEnd = E(pr.box) - E(r.box);
      const dC = C(d.box) - C(pd.box), rC = C(r.box) - C(pr.box);
      const sizeD = ax === 'x' ? d.box.w : d.box.h, sizeR = ax === 'x' ? r.box.w : r.box.h;
      let delta, design, real, kind, base;
      if (prev) {
        design = S(d.box) - E(prev.d.box); real = S(r.box) - E(prev.r.box); delta = real - design; kind = 'prev'; base = design;
        // right / bottom aligned, last on the axis: the end gap is the designed value
        if (Math.round(Math.abs(delta)) > 1 && !hasNext && Math.abs(rEnd - dEnd) <= 1) delta = 0;
        // both moved together with a parent that is centred or stretched: the offset inside the parent is unchanged
      } else {
        const cands = [['start', dStart, rStart], ['end', dEnd, rEnd], ['center', dC, rC]];
        // a text that differs in width keeps only the edge it is aligned on
        let best = cands[0];
        for (const c of cands) if (Math.abs(c[2] - c[1]) < Math.abs(best[2] - best[1])) best = c;
        [kind, design, real] = best; delta = real - design; base = kind === 'center' ? 0 : design;
      }
      // a difference of the element's own size shows on one edge only; the other edge is reported as size, not as spacing
      const lv = lvlPx(delta, shareBase(base));
      if (lv !== 'OK') add(p, ax === 'x' ? 'gapX' : 'gapY', lv, Math.round(design * 10) / 10, Math.round(real * 10) / 10, { delta: Math.round(delta * 10) / 10, kind, prev: prev ? prev.d.id : undefined, sizeDelta: sizeR - sizeD });
    }
    // ---- size
    const linesDiffer = d.type === 'text' && d.lines !== r.lines;
    if (linesDiffer) add(p, 'lines', 'MINOR', d.lines + ' dòng', r.lines + ' dòng', { delta: r.box.h - d.box.h, axis: 'y' });
    const fontDiffers = d.type === 'text' && (d.font.size !== r.font.size || String(d.font.weight) !== String(r.font.weight) || d.font.family !== r.font.family || (d.font.ls || '0') !== (r.font.ls || '0') || (d.font.style || '') !== (r.font.style || ''));
    if (!textDiffers && !linesDiffer && !pageLevel && !fontDiffers) {
      const lw = lvlPx(r.box.w - d.box.w, d.box.w), lh = lvlPx(r.box.h - d.box.h, d.box.h);
      // a text of the same font that measures differently: the font draws differently (file, hinting), a MINOR item
      if (lw !== 'OK') add(p, 'w', d.type === 'text' && fontSame ? 'MINOR' : lw, Math.round(d.box.w * 10) / 10, Math.round(r.box.w * 10) / 10, { delta: Math.round((r.box.w - d.box.w) * 10) / 10, metric: d.type === 'text' && fontSame });
      if (lh !== 'OK') add(p, 'h', d.type === 'text' && fontSame ? 'MINOR' : lh, Math.round(d.box.h * 10) / 10, Math.round(r.box.h * 10) / 10, { delta: Math.round((r.box.h - d.box.h) * 10) / 10, metric: d.type === 'text' && fontSame });
    }
    // ---- text
    if (d.type === 'text') {
      if (textDiffers) add(p, 'text', 'BAD', d.text, r.text);
      const ds = d.font.size - r.font.size;
      if (ds) add(p, 'fontSize', Math.abs(ds) >= T.fontPx ? 'BAD' : 'MINOR', d.font.size + 'px', r.font.size + 'px');
      if (String(d.font.weight) !== String(r.font.weight)) add(p, 'fontWeight', 'BAD', d.font.weight, r.font.weight);
      if (d.font.family !== r.font.family) add(p, 'fontFamily', 'BAD', d.font.family, r.font.family);
      if ((d.font.style || 'normal') !== (r.font.style || 'normal')) add(p, 'fontStyle', 'MINOR', d.font.style || 'normal', r.font.style || 'normal');
      if ((d.font.deco || 'none') !== (r.font.deco || 'none')) add(p, 'textDecoration', 'MINOR', d.font.deco || 'none', r.font.deco || 'none');
      if ((d.font.ls || '0') !== (r.font.ls || '0') && Math.abs(parseFloat(d.font.ls || 0) - parseFloat(r.font.ls || 0)) >= 0.2) add(p, 'letterSpacing', 'MINOR', d.font.ls || '0', r.font.ls || '0');
      if (dist(d.color, r.color) > 2) {
        let lv = lvlColor(d.color, r.color, 'text');
        const big = (f) => f.size >= 24 || (f.size >= 18.66 && +f.weight >= 700);
        const need = (f) => (big(f) ? 3 : 4.5);
        const cd = contrast(d.color, d.under), cr = contrast(r.color, r.under);
        if (cr < need(r.font) && cd >= need(d.font)) lv = 'BAD';
        add(p, 'color', lv, colorText(d.color, 'text'), colorText(r.color, 'text'), { contrast: [Math.round(cd * 100) / 100, Math.round(cr * 100) / 100] });
      }
      // cut without "…" is broken layout (BAD); an ellipsis where the design wraps (or the reverse) is another overflow rule (MINOR)
      if ((d.cut || '') !== (r.cut || '')) add(p, 'cut', r.cut === 'clipped' ? 'BAD' : 'MINOR', d.cut ? (d.cut === 'ellipsis' ? 'cắt có …' : 'bị cắt') : 'hiện đủ', r.cut ? (r.cut === 'ellipsis' ? 'cắt có …' : 'bị cắt') : 'hiện đủ');
      if (d.mixed && r.mixed && d.mixed.length === r.mixed.length) d.mixed.forEach((a, i) => { const b = r.mixed[i]; if (String(a.weight) !== String(b.weight) || a.size !== b.size) add(p, 'fontRun', 'BAD', `"${short(a.text, 20)}" ${a.size}px/${a.weight}`, `"${short(b.text, 20)}" ${b.size}px/${b.weight}`); else if (dist(a.color, b.color) > 2) add(p, 'colorRun', lvlColor(a.color, b.color, 'text'), `"${short(a.text, 20)}" ${colorText(a.color, 'text')}`, `"${short(b.text, 20)}" ${colorText(b.color, 'text')}`); });
      else if (!!d.mixed !== !!r.mixed) add(p, 'fontRun', 'MINOR', d.mixed ? 'nhiều kiểu chữ trong dòng' : 'một kiểu chữ', r.mixed ? 'nhiều kiểu chữ trong dòng' : 'một kiểu chữ');
    } else {
      // ---- role, name, state
      // a field or a tick box against something else is a different control; press-kinds are interchangeable
      const kd = kindOf(d), kr = kindOf(r);
      if (kd !== kr && [kd, kr].some(k => k === 'c:field' || k === 'c:check')) add(p, 'role', 'BAD', d.role ? ROLE_VI[d.role] || d.role : 'không phải ô nhập', r.role ? ROLE_VI[r.role] || r.role : 'không phải ô nhập');
      // states a user sees and both sides hold natively; "selected / expanded" are ARIA marks, their look is compared as paint
      const sdv = d.state || {}, srv = r.state || {};
      for (const k of ['disabled', 'checked', 'readonly']) if (!!sdv[k] !== !!srv[k]) add(p, 'state:' + k, 'BAD', sdv[k] ? 'có' : 'không', srv[k] ? 'có' : 'không');
      const stateDiffers = ['disabled', 'checked', 'readonly'].some(k => !!sdv[k] !== !!srv[k]);
      if (opts.focus && !!sdv.focused !== !!srv.focused) add(p, 'state:focused', 'MINOR', sdv.focused ? 'đang focus' : 'không focus', srv.focused ? 'đang focus' : 'không focus');
      if ((sdv.value ?? '') !== (srv.value ?? '')) add(p, 'state:value', 'BAD', sdv.value || '(trống)', srv.value || '(trống)');
      if ((sdv.placeholder || '') !== (srv.placeholder || '')) add(p, 'placeholder', 'BAD', sdv.placeholder || '(không)', srv.placeholder || '(không)');
      // inside a field: where the typed text (or the placeholder) starts, and the colour of the placeholder
      if (sdv.textX !== undefined && srv.textX !== undefined) { const a = sdv.textX - d.box.x, b = srv.textX - r.box.x; const lv = lvlPx(b - a, a); if (lv !== 'OK') add(p, 'textInset', lv, Math.round(a * 10) / 10, Math.round(b * 10) / 10, { delta: b - a }); }
      // a design that leaves the placeholder's colour to the browser draws whatever that browser draws: nothing to hold
      // the web to
      if (sdv.placeholderColor && srv.placeholderColor && !sdv.placeholderDefault && dist(sdv.placeholderColor, srv.placeholderColor) > 2) add(p, 'placeholderColor', lvlColor(sdv.placeholderColor, srv.placeholderColor, 'text'), colorText(sdv.placeholderColor, 'text'), colorText(srv.placeholderColor, 'text'));
      // ---- paint
      const pa = d.paint || {}, pb = r.paint || {};
      // a fill that is there on one side only is compared with what shows through on the other side (the colour behind)
      if (!!pa.bg !== !!pb.bg || (pa.bg && dist(pa.bg, pb.bg) > 2)) {
        const seenD = pa.bg ? blend(pa.bg, d.under || [255, 255, 255, 1]).concat([1]) : d.under || r.under, seenR = pb.bg ? blend(pb.bg, r.under || [255, 255, 255, 1]).concat([1]) : r.under || d.under;
        // a control that is filled on one side only is in another state or variant (selected tab, primary button): BAD
        const oneSided = !!pa.bg !== !!pb.bg && (d.role || r.role);
        add(p, 'background', oneSided ? 'BAD' : seenD && seenR ? lvlColor(seenD, seenR, 'bg') : 'MINOR', pa.bg ? colorText(pa.bg, 'bg') : seenD ? `không tô (thấy nền ${hex(seenD)})` : colorText(null, 'bg'), pb.bg ? colorText(pb.bg, 'bg') : seenR ? `không tô (thấy nền ${hex(seenR)})` : colorText(null, 'bg'));
      }
      if ((pa.bgImage || '') !== (pb.bgImage || '')) add(p, 'backgroundImage', 'MINOR', pa.bgImage || 'không', pb.bgImage || 'không');
      const SIDE = ['trên', 'phải', 'dưới', 'trái'];
      const ba = pa.border || [null, null, null, null], bb = pb.border || [null, null, null, null];
      const sideDiffs = [];
      for (let i = 0; i < 4; i++) {
        const a = ba[i], b = bb[i];
        if (!a && !b) continue;
        if (!a || !b) { sideDiffs.push([i, 'MINOR', a ? `${a.w}px ${colorText(a.c, 'border')}` : 'không viền', b ? `${b.w}px ${colorText(b.c, 'border')}` : 'không viền']); continue; }
        if (Math.abs(a.w - b.w) >= 0.5) sideDiffs.push([i, 'MINOR', a.w + 'px', b.w + 'px']);
        else if (dist(a.c, b.c) > 2) sideDiffs.push([i, lvlColor(a.c, b.c), colorText(a.c, 'border'), colorText(b.c, 'border')]);
      }
      if (sideDiffs.length) {
        const allSame = sideDiffs.length === 4 && sideDiffs.every(s => s[2] === sideDiffs[0][2] && s[3] === sideDiffs[0][3]);
        if (allSame) add(p, 'border', sideDiffs[0][1], sideDiffs[0][2], sideDiffs[0][3]);
        else for (const [i, lv, a, b] of sideDiffs) add(p, 'border:' + SIDE[i], lv, a, b);
      }
      const ra = pa.radius || [0, 0, 0, 0], rb = pb.radius || [0, 0, 0, 0];
      // a radius only shows on an element that paints; a radius larger than half the box is a pill either way
      const rounded = (q) => !!(q.bg || q.shadow || (q.border && q.border.every(Boolean)));
      if (rounded(pa) && rounded(pb)) {
        const cap = (v, b) => Math.min(v, Math.min(b.w, b.h) / 2);
        const dr = Math.max(...ra.map((v, i) => Math.abs(cap(v, d.box) - cap(rb[i], r.box))));
        if (dr > 1) add(p, 'radius', dr > T.radius ? 'BAD' : 'MINOR', ra.every(v => v === ra[0]) ? ra[0] + 'px' : ra.join('/') + 'px', rb.every(v => v === rb[0]) ? rb[0] + 'px' : rb.join('/') + 'px');
      }
      if ((pa.shadow || '') !== (pb.shadow || '')) add(p, 'shadow', 'MINOR', pa.shadow || 'không bóng', pb.shadow || 'không bóng');
      if ((pa.outline || '') !== (pb.outline || '')) add(p, 'outline', 'MINOR', pa.outline || 'không', pb.outline || 'không');
      if (d.graphic && r.graphic) {
        if (d.graphic.name && r.graphic.name && d.graphic.name !== r.graphic.name) add(p, 'icon', 'BAD', d.graphic.name, r.graphic.name);
        if (d.graphic.color && r.graphic.color && dist(d.graphic.color, r.graphic.color) > 2) add(p, 'iconColor', lvlColor(d.graphic.color, r.graphic.color, 'text'), colorText(d.graphic.color, 'text'), colorText(r.graphic.color, 'text'));
      }
    }
    if (Math.abs((d.opacity ?? 1) - (r.opacity ?? 1)) >= 0.1) add(p, 'opacity', 'MINOR', d.opacity ?? 1, r.opacity ?? 1);
  }

  // ---- cascades: a size difference that a descendant already explains is not a second difference
  const kids = new Map();
  for (const p of pairs) { if (!kids.has(p.pd.id)) kids.set(p.pd.id, []); kids.get(p.pd.id).push(p); }
  const explained = (p, axis, delta) => {
    const stack = [...(kids.get(p.d.id) || [])];
    while (stack.length) {
      const q = stack.pop();
      for (const f of q.diffs) {
        const ax = f.prop === 'w' || f.prop === 'gapX' ? 'x' : f.prop === 'h' || f.prop === 'gapY' || f.prop === 'lines' ? 'y' : null;
        if (ax === axis && f.delta && Math.sign(f.delta) === Math.sign(delta) && Math.abs(f.delta) >= Math.abs(delta) - 1.5) return q;
      }
      stack.push(...(kids.get(q.d.id) || []));
    }
    return null;
  };
  // a container that holds something missing or extra changes size because of it
  const holdsUnmatched = new Set();
  for (const n of D.nodes) if (!m.has(n.id)) for (const id of chain(n, dBy)) holdsUnmatched.add(id);
  for (const n of R.nodes) if (!rev.has(n.id)) for (const id of chain(n, rBy)) if (rev.has(id)) holdsUnmatched.add(rev.get(id));
  for (const p of pairs) if (holdsUnmatched.has(p.d.id)) for (const f of p.diffs) if (f.prop === 'w' || f.prop === 'h' || f.prop === 'textInset') f.cascade = 'content';
  // deepest first: a parent is checked after its children keep or lose their own differences
  const depth = (n) => chain(n, dBy).length;
  for (const p of pairs.slice().sort((a, b) => depth(b.d) - depth(a.d))) {
    for (const f of p.diffs) {
      if ((f.prop !== 'w' && f.prop !== 'h') || f.cascade === 'content') continue;
      const q = explained(p, f.prop === 'w' ? 'x' : 'y', f.delta);
      if (q) { f.cascade = q.d.id; }
    }
    // spacing that only mirrors the element's own size difference on a centred or end-aligned element
    for (const f of p.diffs) {
      if (f.prop !== 'gapX' && f.prop !== 'gapY') continue;
      const own = p.diffs.find(g => g.prop === (f.prop === 'gapX' ? 'w' : 'h') && !g.cascade);
      if (own && f.kind !== 'prev' && Math.abs(Math.abs(f.delta) - Math.abs(own.delta) / (f.kind === 'center' ? 2 : 1)) <= 1) f.cascade = p.d.id;
    }
  }
  // spacing after a neighbour is not a difference of this element when the parent's size change explains it (space-between rows)
  for (const p of pairs) for (const f of p.diffs) {
    if ((f.prop !== 'gapX' && f.prop !== 'gapY') || f.kind !== 'prev' || f.cascade) continue;
    const par = pairOf.get(p.pd.id);
    const pf = par && par.diffs.find(g => g.prop === (f.prop === 'gapX' ? 'w' : 'h'));
    const E = f.prop === 'gapX' ? R_ : B_;
    const endSame = Math.abs((E(p.pr.box) - E(p.r.box)) - (E(p.pd.box) - E(p.d.box))) <= 1;
    if (pf && pf.cascade === undefined && endSame && Math.abs(Math.abs(pf.delta) - Math.abs(f.delta)) <= 1.5) f.cascade = par.d.id;
  }
  // spacing that changed because something extra (or missing) sits in the gap: the extra or missing element is the finding
  {
    const unD = D.nodes.filter(n => !m.has(n.id) && !n.page), unR = R.nodes.filter(n => !rev.has(n.id) && !n.page && (n.type === 'text' || n.paint || n.graphic));
    const between = (list, a0, a1, c0, c1, ax) => list.some(n => { const b = n.box; const s0 = ax === 'x' ? b.x : b.y, s1 = ax === 'x' ? R_(b) : B_(b), o0 = ax === 'x' ? b.y : b.x, o1 = ax === 'x' ? B_(b) : R_(b); return s0 >= a0 - 1 && s1 <= a1 + 1 && overlap(o0, o1, c0, c1) > 0.5; });
    // the text of a field starts elsewhere because an icon inside the field is missing or extra
    const inside = (b, o) => b.x >= o.x - 1 && b.y >= o.y - 1 && R_(b) <= R_(o) + 1 && B_(b) <= B_(o) + 1;
    for (const p of pairs) for (const f of p.diffs) if (f.prop === 'textInset' && (unD.some(n => inside(n.box, p.d.box)) || unR.some(n => inside(n.box, p.r.box)))) f.cascade = 'content';
    for (const p of pairs) for (const f of p.diffs) {
      if ((f.prop !== 'gapX' && f.prop !== 'gapY') || f.cascade !== undefined || f.kind === 'end' || f.kind === 'center') continue;
      const ax = f.prop === 'gapX' ? 'x' : 'y';
      const S0 = (b) => (ax === 'x' ? b.x : b.y), E0 = ax === 'x' ? R_ : B_, O0 = (b) => (ax === 'x' ? b.y : b.x), O1 = ax === 'x' ? B_ : R_;
      const pv = f.prev !== undefined ? pairOf.get(f.prev) : null;
      const rFrom = pv ? E0(pv.r.box) : S0(p.pr.box), dFrom = pv ? E0(pv.d.box) : S0(p.pd.box);
      if (between(unR, rFrom, S0(p.r.box), O0(p.r.box), O1(p.r.box), ax) || between(unD, dFrom, S0(p.d.box), O0(p.d.box), O1(p.d.box), ax)) f.cascade = 'content';
    }
  }
  // no difference may vanish: a cascade must end at an element that still reports a difference on that axis
  const axisOf = (prop) => (prop === 'w' || prop === 'gapX' ? 'x' : prop === 'h' || prop === 'gapY' || prop === 'lines' ? 'y' : null);
  for (let again = true, n = 0; again && n < 5; n++) {
    again = false;
    for (const p of pairs) for (const f of p.diffs) {
      if (f.cascade === undefined || f.cascade === p.d.id || f.cascade === 'content') continue;
      const t = pairOf.get(f.cascade);
      // the target still reports it, or the target itself holds something missing or extra (reported as such)
      if (!t || !t.diffs.some(g => (g.cascade === undefined || g.cascade === 'content') && axisOf(g.prop) === axisOf(f.prop))) { delete f.cascade; again = true; }
    }
  }

  // ---- missing and extra: only the top-most unmatched node of a missing block is reported
  const out = [];
  const pct = (b, S) => ({ x: +(Math.max(0, b.x - S.scroll.x) / S.viewport.w * 100).toFixed(2), y: +(Math.max(0, b.y - S.scroll.y) / S.viewport.h * 100).toFixed(2), w: +(Math.max(Math.min(b.w, S.viewport.w), 1) / S.viewport.w * 100).toFixed(2), h: +(Math.max(Math.min(b.h, S.viewport.h), 1) / S.viewport.h * 100).toFixed(2) });
  const unmatched = (S, by, has, ft, side) => {
    const un = S.nodes.filter(n => !has(n.id));
    // a page-level element (the page background) that found no counterpart is not a block that hides what is inside it
    const set = new Set(un.filter(n => !n.page).map(n => n.id));
    for (const n of un) {
      if (chain(n, by).some(id => set.has(id))) continue;   // inside a reported block
      if (n.page) continue;   // the page background or frame: the prototypes paint it on different elements
      const sub = un.filter(x => x === n || chain(x, by).includes(n.id));
      if (!n.paint && !n.graphic && n.type === 'el' && sub.length === 1) continue;
      const functional = sub.some(x => x.type === 'text' || x.role || (x.graphic && x.graphic.kind !== 'svg'));
      const iconInControl = n.graphic && n.graphic.kind === 'svg' && by.get(n.parent) && by.get(n.parent).role && !ft.get(n.parent);
      const level = functional || iconInControl ? 'BAD' : 'MINOR';
      out.push({ id: `${side}${n.id}`, element: label(n, ft), prop: side === 'd' ? 'missing' : 'extra', type: 'MISSING_ELEMENT', level,
        design: side === 'd' ? 'có' : 'không có', actual: side === 'd' ? 'không có' : 'có', box: side === 'd' ? undefined : pct(n.box, R), design_box: side === 'd' ? pct(n.box, D) : undefined,
        px: n.box, inside: sub.length - 1, dNode: side === 'd' ? n.id : undefined, rNode: side === 'r' ? n.id : undefined, chartOf: n.chart ? (side === 'd' ? n.parent : (rev.get(n.parent) ?? n.parent)) : undefined });
    }
  };
  unmatched(D, dBy, id => m.has(id), ftD, 'd');
  unmatched(R, rBy, id => rev.has(id), ftR, 'r');

  const TYPE = { gapX: 'SPACING', gapY: 'SPACING', w: 'SIZE', h: 'SIZE', lines: 'SIZE', text: 'MISSING_ELEMENT', fontSize: 'FONT', fontWeight: 'FONT', fontFamily: 'FONT', fontStyle: 'FONT', textDecoration: 'FONT', letterSpacing: 'FONT', fontRun: 'FONT', color: 'FONT', colorRun: 'FONT', cut: 'SIZE', background: 'BACKGROUND_COLOR', backgroundImage: 'BACKGROUND_COLOR', border: 'BACKGROUND_COLOR', radius: 'RADIUS', shadow: 'BACKGROUND_COLOR', outline: 'BACKGROUND_COLOR', icon: 'MISSING_ELEMENT', iconColor: 'BACKGROUND_COLOR', opacity: 'BACKGROUND_COLOR', role: 'MISSING_ELEMENT', name: 'MISSING_ELEMENT', placeholder: 'MISSING_ELEMENT', textInset: 'SPACING', placeholderColor: 'FONT' };
  const PROP_VI = { gapX: 'khoảng cách ngang', gapY: 'khoảng cách dọc', w: 'rộng', h: 'cao', lines: 'số dòng', text: 'chữ', fontSize: 'cỡ chữ', fontWeight: 'độ đậm', fontFamily: 'font', fontStyle: 'kiểu chữ', textDecoration: 'gạch chân', letterSpacing: 'giãn chữ', fontRun: 'kiểu chữ trong dòng', color: 'màu chữ', colorRun: 'màu chữ trong dòng', cut: 'cắt chữ', background: 'màu nền', backgroundImage: 'ảnh nền', border: 'viền', radius: 'bo góc', shadow: 'bóng', outline: 'vòng focus', icon: 'icon', iconColor: 'màu icon', opacity: 'độ mờ', role: 'loại điều khiển', name: 'tên', placeholder: 'chữ gợi ý', textInset: 'chỗ chữ bắt đầu trong ô', placeholderColor: 'màu chữ gợi ý' };
  const KIND_VI = { prev: 'tới phần tử trước', start: 'tới mép đầu khung', end: 'tới mép cuối khung', center: 'so với giữa khung' };
  const cascades = [];
  for (const p of pairs) for (const f of p.diffs) {
    const base = f.prop.split(':')[0];
    const item = { id: `p${p.d.id}:${f.prop}`, element: label(p.r, ftR), prop: f.prop, type: TYPE[base] || (base === 'state' ? 'MISSING_ELEMENT' : 'SIZE'), level: f.level,
      what: (PROP_VI[base] || (base === 'state' ? 'trạng thái ' + f.prop.split(':')[1] : base)) + (f.prop.includes(':') && base === 'border' ? ' ' + f.prop.split(':')[1] : '') + (f.kind === 'prev' && pairOf.get(f.prev) ? ' tới ' + label(pairOf.get(f.prev).r, ftR) + (f.prop === 'gapY' ? ' ở trên' : ' bên trái') : f.kind ? ' ' + KIND_VI[f.kind] : ''),
      design: typeof f.design === 'number' ? f.design + 'px' : String(f.design), actual: typeof f.real === 'number' ? f.real + 'px' : String(f.real), delta: f.delta,
      box: pct(p.r.box, R), design_box: pct(p.d.box, D), px: p.r.box, dNode: p.d.id, rNode: p.r.id, metric: f.metric || undefined, contrast: f.contrast, chartOf: p.d.chart || p.r.chart ? p.pd.id : undefined };
    if (f.cascade !== undefined) { item.cascadeOf = f.cascade; cascades.push(item); } else out.push(item);
  }
  // The reference can break with data the designer never tried (a long name wraps and pushes a block out of the fixed
  // frame). A design element cut by the frame, and everything in the block that holds it, is not a base to judge on.
  const cutByFrame = D.nodes.filter(n => n.full && !n.page && (n.full.y + n.full.h > D.viewport.h + 0.5 || n.full.x + n.full.w > D.viewport.w + 0.5) && !n.pos);
  if (cutByFrame.length) {
    const bad = new Set();
    for (const n of cutByFrame) { bad.add(n.id); for (const x of D.nodes) if (chain(x, dBy).includes(n.id)) bad.add(x.id); }
    const badR = new Set([...bad].filter(id => m.has(id)).map(id => m.get(id)));
    for (const d of out) {
      const inD = d.dNode !== undefined && bad.has(d.dNode);
      const inR = d.rNode !== undefined && (badR.has(d.rNode) || chain(rBy.get(d.rNode), rBy).some(id => badR.has(id)));
      if (inD || inR) { d.refBroken = true; d.level = 'MINOR'; }
    }
  }
  // Font metrics: when several texts of the same font measure differently (the font files differ, seen on Firefox),
  // small vertical differences elsewhere on the page follow from it. They are folded into that one finding.
  if (out.filter(d => d.metric && d.prop === 'h').length >= 2) for (const d of out) if (!d.metric && (d.prop === 'gapY' || d.prop === 'h') && d.level === 'MINOR') { d.metric = true; d.follows = true; }
  return { diffs: out, cascades, refBroken: cutByFrame.length > 0, pairs: pairs.length, unmatchedD: D.nodes.filter(n => !m.has(n.id)).length, unmatchedR: R.nodes.filter(n => !rev.has(n.id)).length };
}

/**
 * Checks that need no design: broken layout on a page (layer B). Run on the real page; run on the reference too, to
 * know when the reference itself is broken by the data.
 */
function general(S) {
  const by = index(S.nodes);
  const out = [];
  const pct = (b) => ({ x: +(Math.max(0, b.x - S.scroll.x) / S.viewport.w * 100).toFixed(2), y: +(Math.max(0, b.y - S.scroll.y) / S.viewport.h * 100).toFixed(2), w: +(Math.max(Math.min(b.w, S.viewport.w), 1) / S.viewport.w * 100).toFixed(2), h: +(Math.max(Math.min(b.h, S.viewport.h), 1) / S.viewport.h * 100).toFixed(2) });
  const name = (n) => (n.type === 'text' ? `Chữ "${n.text.slice(0, 40)}"` : n.role ? `${ROLE_VI[n.role] || n.role} "${(n.name || '').slice(0, 40)}"` : `Khung ${Math.round(n.box.w)}×${Math.round(n.box.h)}`);
  const push = (n, what, design, actual) => out.push({ id: `g${n ? n.id : 0}:${what}`, element: n ? name(n) : 'Trang', prop: 'general', what, type: 'SIZE', level: 'BAD', design, actual, box: n ? pct(n.box) : { x: 0, y: 0, w: 100, h: 100 }, px: n ? n.box : { x: 0, y: 0, w: S.viewport.w, h: S.viewport.h }, rNode: n ? n.id : undefined });
  if (S.doc.w > S.viewport.w + 1) push(null, 'cuộn ngang', `rộng ${S.viewport.w}px`, `nội dung rộng ${S.doc.w}px`);
  for (const n of S.nodes) {
    if (n.type === 'text' && n.cut === 'clipped') push(n, 'chữ bị cắt không có …', 'hiện đủ hoặc có …', 'bị cắt');
    // something drawn past the right or left edge of the window, that scrolling cannot reach (a fixed layer) or that makes the page scroll sideways
    const f = n.full || n.box;
    if (!n.page && n.type === 'el' && (f.x + f.w > S.viewport.w + 1 || f.x < -1) && f.w <= S.viewport.w) push(n, 'tràn ra ngoài mép màn hình', 'nằm trong màn hình', `x ${Math.round(f.x)}..${Math.round(f.x + f.w)} / màn ${S.viewport.w}px`);
  }
  // two texts drawn on top of each other, inside the same box (a dialog over the page is a layer, not an overlap)
  const texts = S.nodes.filter(n => n.type === 'text');
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    if (texts[i].parent !== texts[j].parent) continue;
    const a = texts[i].box, b = texts[j].box;
    const ox = overlap(a.x, R_(a), b.x, R_(b)), oy = overlap(a.y, B_(a), b.y, B_(b));
    if (ox > 2 && oy > 2 && ox * oy > 0.3 * Math.min(a.w * a.h, b.w * b.h)) { push(texts[j], 'chữ chồng lên chữ khác', 'không chồng', `chồng lên "${texts[i].text.slice(0, 30)}"`); if (texts[j].chart) out[out.length - 1].chartOf = texts[j].parent; }
  }
  return out;
}

/** Same difference on many like elements (every row of a table, every text of one font) = one finding with a count. */
function group(diffs) {
  const g = new Map();
  for (const d of diffs) {
    const key = d.chartOf !== undefined ? `chart|${d.chartOf}` : d.metric ? `metric|${d.prop === 'w' ? 'x' : 'y'}` : `${d.prop}|${d.design}|${d.actual}|${d.element.replace(/"[^"]*"/, '""')}`;
    if (!g.has(key)) g.set(key, { ...d, count: 0, members: [] });
    const e = g.get(key); e.count++; e.members.push(d.id); e.level = worst(e.level, d.level);
    if (d.chartOf !== undefined) { e.element = 'Biểu đồ'; e.what = 'chữ trong biểu đồ (chú giải, nhãn trục)'; e.design = 'như design'; e.actual = `${e.count} chỗ khác (thiếu, thừa hoặc lệch chỗ)`; }
    else if (d.metric && e.count > 1) { e.element = 'Chữ (số đo font)'; e.what = d.prop === 'w' ? 'rộng' : 'cao và khoảng cách dọc kéo theo'; e.design = 'cùng font, cùng chữ'; e.actual = `${e.count} chỗ đo khác`; }
  }
  return [...g.values()];
}
const levelOf = (diffs) => diffs.reduce((a, d) => worst(a, d.level), 'OK');

module.exports = { PRESETS, setThresholds, thresholds, setTokens, loadTokens, prefixOf, matchNodes, compare, general, group, levelOf, tokenOf, colorText, hex, lvlPx, lvlColor, contrast, kindOf };
