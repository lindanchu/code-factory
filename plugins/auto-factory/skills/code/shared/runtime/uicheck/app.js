// The app side of the UI check: what extract.js reads from a web page, read from an app on a virtual phone through
// Appium (a WebdriverIO session). Only the reading differs; the design side and the comparison are the web's.
//
// A shipped build (an AAB, an APK, a simulator build) shows no style sheet: a phone tells, through its accessibility
// tree, where each element is, what it says and what it is (a button, a field, a switch...). The colours are read from
// the screenshot. What neither can tell (the font, its size and weight, the corners, borders and shadows of a box) is
// not measured on an app: it is taken from the matched design element, so it is never reported as a difference, and
// the AI look, which sees both images, judges it (LIMITS goes to the look as a limit of the measurement).
//
// Sizes are in points (iOS) and dp (Android): the design page is opened at the phone's size in those units, with its
// pixel ratio, so one CSS px of the design is one point / dp of the phone.
const { decode } = require('./annotate');

const LIMITS = [
  'Trên app (bản build thật) không đọc được font, cỡ chữ, độ đậm, bo góc, viền và bóng: các thuộc tính này không đo, AI xem trên cặp ảnh.',
  'Ảnh app có thanh trạng thái và thanh điều hướng của máy; khung so là màn của app.',
  'Khung chỉ tô nền (thẻ, panel) không có trong cây giao diện của app: thiếu khung như vậy không tính là khác biệt, AI xem trên cặp ảnh.',
];

const isApp = (t) => !!t && typeof t.getPageSource === 'function' && typeof t.takeScreenshot === 'function';

// ---------------------------------------------------------------- the page source (XML) of Appium
function parseXml(xml) {
  const root = { tag: '#root', attrs: {}, children: [] };
  const stack = [root];
  const re = /<(\/?)([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|<\?[\s\S]*?\?>|<!--[\s\S]*?-->/g;
  const attrRe = /([\w.:-]+)\s*=\s*("([^"]*)"|'([^']*)')/g;
  const unesc = (s) => s.replace(/&(lt|gt|amp|quot|apos|#\d+|#x[0-9a-f]+);/gi, (m, e) => (e === 'lt' ? '<' : e === 'gt' ? '>' : e === 'amp' ? '&' : e === 'quot' ? '"' : e === 'apos' ? "'"
    : e[1] === 'x' || e[1] === 'X' ? String.fromCodePoint(parseInt(e.slice(2), 16)) : String.fromCodePoint(parseInt(e.slice(1), 10))));
  let m;
  while ((m = re.exec(xml))) {
    if (!m[2]) continue;
    if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {};
    let a;
    attrRe.lastIndex = 0;
    while ((a = attrRe.exec(m[3] || ''))) attrs[a[1]] = unesc(a[3] !== undefined ? a[3] : a[4]);
    const node = { tag: m[2], attrs, children: [] };
    stack[stack.length - 1].children.push(node);
    if (!m[4]) stack.push(node);
  }
  return root;
}

const ANDROID_ROLE = [
  [/EditText|AutoCompleteTextView/, 'textbox'], [/CheckBox|CheckedTextView/, 'checkbox'], [/Switch|ToggleButton/, 'switch'],
  [/RadioButton/, 'radio'], [/SeekBar/, 'slider'], [/Spinner/, 'combobox'], [/Button/, 'button'],
];
const IOS_ROLE = {
  XCUIElementTypeButton: 'button', XCUIElementTypeTextField: 'textbox', XCUIElementTypeSecureTextField: 'textbox',
  XCUIElementTypeSearchField: 'searchbox', XCUIElementTypeTextView: 'textbox', XCUIElementTypeSwitch: 'switch',
  XCUIElementTypeToggle: 'switch', XCUIElementTypeLink: 'link', XCUIElementTypeTab: 'tab', XCUIElementTypeSlider: 'slider',
  XCUIElementTypeCheckBox: 'checkbox', XCUIElementTypeRadioButton: 'radio', XCUIElementTypePickerWheel: 'combobox',
  XCUIElementTypeMenuItem: 'menuitem', XCUIElementTypeSegmentedControl: 'tablist',
};
const IOS_TEXT = /^XCUIElementType(StaticText)$/;
const IOS_GRAPHIC = /^XCUIElementType(Image|Icon)$/;

/** One element of the tree, read the same way on both platforms. Box in points / dp. */
function readEl(n, platform, scale) {
  const A = n.attrs;
  if (platform === 'android') {
    const b = /\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]/.exec(A.bounds || '');
    if (!b) return null;
    const [x1, y1, x2, y2] = b.slice(1).map(Number);
    const cls = A.class || n.tag;
    let role = null;
    for (const [re, r] of ANDROID_ROLE) if (re.test(cls)) { role = r; break; }
    const clickable = A.clickable === 'true' || A['long-clickable'] === 'true';
    if (!role && A.checkable === 'true') role = 'checkbox';
    if (!role && clickable) role = 'button';
    const label = A['content-desc'] || '';
    return { cls, role, clickable, shown: A.displayed !== 'false', enabled: A.enabled !== 'false',
      box: { x: x1 / scale, y: y1 / scale, w: (x2 - x1) / scale, h: (y2 - y1) / scale },
      text: role === 'textbox' ? '' : (A.text || ''), label, value: role === 'textbox' ? (A.text || '') : '', hint: A.hint || '',
      password: A.password === 'true', checked: A.checkable === 'true' ? A.checked === 'true' : undefined,
      selected: A.selected === 'true', focused: A.focused === 'true',
      graphic: /ImageView|ImageButton/.test(cls) ? 'img' : null,
      isText: /TextView$/.test(cls) && !role, webview: /WebView/.test(cls) };
  }
  const type = A.type || n.tag;
  if (!('x' in A)) return null;
  const role = IOS_ROLE[type] || null;
  const label = A.label || A.name || '';
  return { cls: type, role, clickable: !!role, shown: A.visible !== 'false', enabled: A.enabled !== 'false',
    box: { x: +A.x, y: +A.y, w: +A.width, h: +A.height },
    text: IOS_TEXT.test(type) ? (A.label || A.value || A.name || '') : '', label, value: role === 'textbox' || role === 'searchbox' ? (A.value || '') : '',
    // Flutter puts a field's hint in its label (no placeholderValue): an empty field shows its label as the hint
    hint: A.placeholderValue || ((role === 'textbox' || role === 'searchbox') && !A.value ? label : ''),
    password: type === 'XCUIElementTypeSecureTextField',
    checked: role === 'switch' || role === 'checkbox' ? A.value === '1' || A.value === 'true' : undefined,
    selected: A.selected === 'true', focused: A.focused === 'true' || A.hasFocus === 'true',
    graphic: IOS_GRAPHIC.test(type) ? 'img' : null, isText: IOS_TEXT.test(type), webview: type === 'XCUIElementTypeWebView' };
}

const norm = (s) => String(s || '').replace(/[\s ]+/g, ' ').trim();

/**
 * The nodes of the screen, in the shape extract.js gives for a web page: el (a control, a graphic, a painted box) and
 * text, each with its box, parent and document order. Fields that cannot be read on an app are listed in
 * `unmeasured` of the node and filled from the design element it is matched with (fillUnmeasured).
 */
function nodesOf(xml, platform, scale, vp, img) {
  const tree = parseXml(xml);
  const nodes = [];
  const add = (n) => { n.id = nodes.length + 1; nodes.push(n); return n.id; };
  const inScreen = (b) => b.w >= 1 && b.h >= 1 && b.x < vp.width && b.y < vp.height && b.x + b.w > 0 && b.y + b.h > 0;
  const page = { type: 'el', tag: 'app', order: 0, parent: 0, box: { x: 0, y: 0, w: vp.width, h: vp.height }, page: true };
  const pageBg = img ? bgOf(img, page.box, scale) : null;
  if (pageBg) page.paint = { bg: pageBg, radius: [0, 0, 0, 0] };
  add(page);
  let order = 0;
  const walk = (n, parentId) => {
    const e = n.tag === '#root' || n.tag === 'hierarchy' ? null : readEl(n, platform, scale);
    let me = parentId;
    if (e && e.shown && inScreen(e.box)) {
      order++;
      const under = img ? bgAround(img, e.box, scale) : null;
      const descendantsText = hasText(n, platform);
      if (e.role) {
        const name = norm(e.label || e.text || e.hint || e.value).slice(0, 100);
        const el = { type: 'el', tag: e.cls, order, parent: parentId, box: e.box, role: e.role, name,
          state: stateOf(e), unmeasured: ['radius', 'border', 'shadow', 'outline'] };
        const fill = img ? bgOf(img, e.box, scale) : null;
        if (fill && under && dist(fill, under) > 12) { el.paint = { bg: fill.concat([1]) }; el.under = under.concat([1]); }
        if (e.graphic) el.graphic = { kind: 'img' };
        me = add(el);
        // the label of a control that holds no text element of its own (a Flutter button exposes only its label): one
        // text, where its ink is
        const shown = norm(e.role === 'textbox' ? (e.value || e.hint) : (e.label || e.text));
        const ink = shown && !descendantsText && !e.graphic && img ? inkOf(img, e.box, scale) : null;
        // a control that shows an icon, not its label (an eye in a password field): its ink is far narrower than any
        // text of that label could be, so the label is only its name, not a text on the screen
        const iconOnly = ink && e.role !== 'textbox' && ink.box.w < Math.max(10, shown.length * 3.2);
        if (e.role === 'textbox' || e.role === 'searchbox') {
          // a field's hint and value are its state (as on the web: no text of their own); the hint's colour is its ink
          if (ink && !e.value && e.hint) el.state.placeholderColor = ink.color;
        } else if (shown && !descendantsText && !e.graphic && !iconOnly) {
          add({ type: 'text', order: order + 0.5, parent: me, box: ink ? ink.box : e.box, text: shown.slice(0, 300), lines: 1,
            font: null, color: ink ? ink.color : null, under: (fill || under || [255, 255, 255]).concat([1]),
            unmeasured: ['font', 'lines', 'box'].concat(ink ? [] : ['color']) });
        }
      } else if (e.graphic) {
        // an image (Flutter's Semantics(image: true) is an ImageView / Image with a label): the image, never a text
        me = add({ type: 'el', tag: e.cls, order, parent: parentId, box: img ? tileOf(img, e.box, scale) : e.box,
          graphic: { kind: 'img' }, name: norm(e.label).slice(0, 100) || undefined, unmeasured: ['radius', 'border', 'shadow'] });
      } else if (e.isText || (platform === 'android' && e.label && !n.children.length)) {
        const text = norm(e.text || e.label);
        if (text) {
          const ink = img ? inkOf(img, e.box, scale) : null;
          me = add({ type: 'text', order, parent: parentId, box: e.box, text: text.slice(0, 300), lines: 1, font: null,
            color: ink ? ink.color : null, under: (bgOf(img, e.box, scale) || under || [255, 255, 255]).concat([1]),
            unmeasured: ['font', 'lines'].concat(ink ? [] : ['color']) });
        }
      } else if (img && under) {
        // a box that paints something (a card, a bar): kept when its own colour differs from what is around it
        const fill = bgOf(img, e.box, scale);
        if (fill && dist(fill, under) > 12 && e.box.w * e.box.h < vp.width * vp.height * 0.95) {
          me = add({ type: 'el', tag: e.cls, order, parent: parentId, box: e.box, paint: { bg: fill.concat([1]) }, under: under.concat([1]),
            unmeasured: ['radius', 'border', 'shadow', 'outline'] });
        }
      }
    }
    for (const c of n.children) walk(c, me);
  };
  walk(tree, 1);
  return nodes;
}

function hasText(n, platform) {
  for (const c of n.children) {
    const A = c.attrs;
    if (platform === 'android' ? (/TextView$/.test(A.class || '') && A.text) || (A['content-desc'] && !c.children.length && A.clickable !== 'true')
      : IOS_TEXT.test(A.type || c.tag) && (A.label || A.value)) return true;
    if (hasText(c, platform)) return true;
  }
  return false;
}

function stateOf(e) {
  const s = {};
  if (e.checked !== undefined) s.checked = e.checked;
  if (!e.enabled) s.disabled = true;
  if (e.selected) s.selected = true;
  if (e.value) s.value = e.password ? '•'.repeat(Math.min(12, e.value.length)) : e.value;
  if (e.hint) s.placeholder = e.hint;
  return s;
}

// ---------------------------------------------------------------- colours from the screenshot
const dist = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
function px(img, x, y) {
  x = Math.max(0, Math.min(img.width - 1, Math.round(x))); y = Math.max(0, Math.min(img.height - 1, Math.round(y)));
  const o = (y * img.width + x) * 4;
  return [img.data[o], img.data[o + 1], img.data[o + 2]];
}
/** The commonest colour of some points (colours within a few steps count as one). */
function mode(points) {
  const count = new Map();
  for (const c of points) { const k = c.map(v => v >> 3).join(','); const o = count.get(k); if (o) { o.n++; o.sum[0] += c[0]; o.sum[1] += c[1]; o.sum[2] += c[2]; } else count.set(k, { n: 1, sum: c.slice() }); }
  let best = null;
  for (const v of count.values()) if (!best || v.n > best.n) best = v;
  return best ? best.sum.map(s => Math.round(s / best.n)) : null;
}
/** The fill of a box: its colour near the middle of each side, inside, past its corners. */
function bgOf(img, b, scale) {
  if (!img) return null;
  const x = b.x * scale, y = b.y * scale, w = b.w * scale, h = b.h * scale;
  if (w < 4 || h < 4) return null;
  const pts = [];
  const ix = Math.max(2, w * 0.12), iy = Math.max(2, h * 0.12);
  for (let i = 0; i <= 8; i++) {
    const t = 0.2 + 0.6 * i / 8;
    pts.push(px(img, x + w * t, y + iy), px(img, x + w * t, y + h - iy), px(img, x + ix, y + h * t), px(img, x + w - ix, y + h * t));
  }
  return mode(pts);
}
/** What is behind a box: the colour just outside it. */
function bgAround(img, b, scale) {
  if (!img) return null;
  const x = b.x * scale, y = b.y * scale, w = b.w * scale, h = b.h * scale, o = 3;
  const pts = [];
  for (let i = 0; i <= 8; i++) {
    const t = 0.1 + 0.8 * i / 8;
    pts.push(px(img, x + w * t, y - o), px(img, x + w * t, y + h + o), px(img, x - o, y + h * t), px(img, x + w + o, y + h * t));
  }
  return mode(pts);
}
/** The ink of a text inside a box: its colour (the strongest pixels against the fill) and where it is (box in pt/dp). */
function inkOf(img, b, scale) {
  const bg = bgOf(img, b, scale);
  if (!bg) return null;
  const x0 = Math.max(0, Math.floor(b.x * scale)), y0 = Math.max(0, Math.floor(b.y * scale));
  const x1 = Math.min(img.width - 1, Math.ceil((b.x + b.w) * scale)), y1 = Math.min(img.height - 1, Math.ceil((b.y + b.h) * scale));
  let max = 0;
  const hits = [];
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) { const c = px(img, x, y), d = dist(c, bg); if (d > 90) { hits.push([x, y, d, c]); if (d > max) max = d; } }
  if (hits.length < 4) return null;
  const strong = hits.filter(h => h[2] >= max * 0.85);
  const color = [0, 1, 2].map(i => Math.round(strong.reduce((s, h) => s + h[3][i], 0) / strong.length)).concat([1]);
  const core = hits.filter(h => h[2] >= max * 0.5);
  const lx = Math.min(...core.map(h => h[0])), rx = Math.max(...core.map(h => h[0])), ty = Math.min(...core.map(h => h[1])), by = Math.max(...core.map(h => h[1]));
  return { color, box: { x: lx / scale, y: ty / scale, w: (rx - lx + 1) / scale, h: (by - ty + 1) / scale } };
}

/**
 * The box of an image as it is seen: an app often labels only the mark inside a painted tile (the letters of a logo
 * on its square), so the box grows to the tile when the image sits on a fill of its own that differs from what is
 * around the tile. A tile wider than 64 pt on a side is not a tile (the image sits on the page): the box stays.
 */
function tileOf(img, b, scale) {
  const tile = bgAround(img, b, scale);
  if (!tile) return b;
  const cx = (b.x + b.w / 2) * scale, cy = (b.y + b.h / 2) * scale, lim = 64 * scale;
  const reach = (x, y, dx, dy, from) => {
    // a box's right / bottom edge is the first pixel past it; its left / top edge is its own first pixel
    const o = dx + dy > 0 ? 0 : 1;
    let n = 0;
    while (n < lim) {
      const c = px(img, x + dx * (from + n + o), y + dy * (from + n + o));
      if (dist(c, tile) > 40) return n;
      n++;
    }
    return -1;
  };
  const hw = b.w * scale / 2, hh = b.h * scale / 2;
  const l = reach(cx, cy, -1, 0, hw), r = reach(cx, cy, 1, 0, hw), t = reach(cx, cy, 0, -1, hh), d = reach(cx, cy, 0, 1, hh);
  if (l < 0 || r < 0 || t < 0 || d < 0 || l + r + t + d === 0) return b;
  return { x: b.x - l / scale, y: b.y - t / scale, w: b.w + (l + r) / scale, h: b.h + (t + d) / scale };
}

// ---------------------------------------------------------------- one capture of the app
/**
 * The app as a user sees it now: { vp, shown, shot, R0 (the nodes), webText, url }. device: what fx / tx said about
 * the phone (its size in pt / dp), when known.
 */
async function capture(app, device) {
  const caps = app.capabilities || {};
  const platform = String(caps.platformName || (device && device.platform) || '').toLowerCase().includes('ios') ? 'ios' : 'android';
  const b64 = await app.takeScreenshot();
  const shot = Buffer.from(b64, 'base64');
  const img = decode(shot);
  const rect = await app.getWindowRect().catch(() => null);
  let vp;
  if (device && Array.isArray(device.size) && device.size[0] > 0) vp = { width: device.size[0], height: device.size[1] };
  else if (platform === 'ios' && rect) vp = { width: Math.round(rect.width), height: Math.round(rect.height) };
  else {
    const info = await app.execute('mobile: deviceInfo').catch(() => ({}));
    const dpi = Number(info.displayDensity) || 160;
    vp = rect ? { width: Math.round(rect.width * 160 / dpi), height: Math.round(rect.height * 160 / dpi) } : { width: 360, height: 640 };
  }
  const scale = img ? img.width / vp.width : (platform === 'ios' ? 3 : 2.625);
  const xml = await app.getPageSource();
  const nodes = nodesOf(xml, platform, scale, vp, img);
  const text = nodes.filter(n => n.type === 'text').map(n => n.text).join(' ');
  const head = (nodes.filter(n => n.type === 'text' && n.box.y < vp.height * 0.3).sort((a, b) => (b.box.h - a.box.h) || (a.box.y - b.box.y))[0] || {}).text || '';
  const intl = Intl.DateTimeFormat().resolvedOptions();
  const shown = { locale: process.env.AUTOTEST_LOCALE || intl.locale, timezoneId: intl.timeZone, deviceScaleFactor: Math.round(scale * 1000) / 1000,
    colorScheme: 'light', width: vp.width, height: vp.height };
  const R0 = { nodes, viewport: { w: vp.width, h: vp.height }, doc: { w: vp.width, h: vp.height, clientW: vp.width }, scroll: { x: 0, y: 0 }, active: null };
  const url = `app://${platform}/${(device && device.key) || ''}`;
  return { platform, vp, scale, shown, shot, img: !!img, R0, webText: { text, head }, url, xml };
}

/** The dialog of an app (an alert, a sheet) is compared as such; otherwise the whole screen. */
function scopeOf(xml, platform) {
  if (platform === 'ios') return /XCUIElementTypeAlert|XCUIElementTypeSheet/.test(xml) ? 'dialog' : 'page';
  return /resource-id="android:id\/(alertTitle|parentPanel)"/.test(xml) ? 'dialog' : 'page';
}

/**
 * What an app cannot tell is taken from the design element each app node is matched with: the font, the number of
 * lines and (for a label read from its ink) the size of its box around the same centre; the corners, borders and
 * shadows of a box. The AI look judges those on the images.
 */
function fillUnmeasured(D, R, match) {
  const dBy = new Map(D.nodes.map(n => [n.id, n]));
  const rev = match.rev || new Map([...match.m].map(([d, r]) => [r, d]));
  for (const r of R.nodes) {
    if (!r.unmeasured) continue;
    const d = dBy.get(rev.get(r.id));
    const un = new Set(r.unmeasured);
    if (r.type === 'text') {
      r.font = d && d.font ? { ...d.font } : r.font || { size: 0, weight: '400', family: '' };
      if (un.has('lines')) r.lines = d && d.lines ? d.lines : 1;
      if (un.has('color') && (!r.color || !d)) r.color = d && d.color ? d.color : r.color || [0, 0, 0, 1];
      if (un.has('box') && d) {
        const cx = r.box.x + r.box.w / 2, cy = r.box.y + r.box.h / 2;
        r.box = { x: cx - d.box.w / 2, y: cy - d.box.h / 2, w: d.box.w, h: d.box.h };
      }
      if (!r.under) r.under = d && d.under ? d.under : [255, 255, 255, 1];
      if (d && d.align) r.align = d.align;
    } else if (d && d.paint) {
      const p = r.paint || {};
      for (const k of ['radius', 'border', 'shadow', 'outline']) if (un.has(k) && d.paint[k] !== undefined) p[k] = d.paint[k];
      if (Object.keys(p).length) r.paint = p;
      if (!r.under && d.under) r.under = d.under;
    }
    if (r.type === 'text' && (!r.color || r.color.length < 3)) r.color = [0, 0, 0, 1];
  }
  // colours read from pixels carry the smoothing of edges and the phone screen's colour profile (a few steps per
  // channel): such a colour is its design element's colour when it is that close (6), or a little further (24) when
  // the token nearest to it is the design colour's own token. A colour nearer to another token stays as read, so a
  // wrong token is still a difference.
  const E = require('./engine');
  const near = (a, b) => {
    if (!a || !b || a.length < 3 || b.length < 3) return false;
    const d = dist(a, b);
    if (d <= 6) return true;
    if (d > 24) return false;
    const mine = E.tokenOf(a), want = E.tokenOf(b);
    return want.exact && mine.names.length > 0 && mine.names.some(n => want.names.includes(n));
  };
  for (const r of R.nodes) {
    const d = dBy.get(rev.get(r.id));
    if (!d) continue;
    if (near(r.color, d.color)) r.color = d.color.slice();
    if (r.paint && d.paint && near(r.paint.bg, d.paint.bg)) r.paint.bg = d.paint.bg.slice();
    if (near(r.under, d.under)) r.under = d.under.slice();
    if (r.state && d.state && near(r.state.placeholderColor, d.state.placeholderColor)) r.state.placeholderColor = d.state.placeholderColor.slice();
  }
  // a text the design has no partner for still needs a font for the comparer to describe it
  for (const r of R.nodes) if (r.type === 'text' && !r.font) r.font = { size: 0, weight: '400', family: '' };
  return R;
}

/**
 * Differences an app cannot show: a box of the design that only paints (a card, a panel) and holds nothing that is
 * missing too. An app's accessibility tree has no node for such a box, so it is "missing" on every screen; the AI look
 * judges it on the two images. A missing box with a text, a control or an image inside stays.
 */
function dropUnseen(diffs, D, match) {
  const found = (match && match.m) || new Map();
  const by = new Map(D.nodes.map(n => [n.id, n]));
  return diffs.filter(d => {
    // MINOR: nothing inside it that a user reads or uses is missing (that would make it BAD)
    if (d.prop !== 'missing' || d.level !== 'MINOR') return true;
    const n = by.get(d.dNode);
    if (!(n && n.type === 'el' && !n.role && !n.graphic)) return true;
    // an icon or an image inside it that is missing with it is something a user sees: the difference stays
    const inside = (x) => { for (let p = by.get(x.parent); p; p = by.get(p.parent)) if (p.id === n.id) return true; return false; };
    return D.nodes.some(x => x.graphic && !found.has(x.id) && inside(x));
  });
}

/**
 * The accessibility problems a screen of an app shows, in the shape axe gives on the web: a control nobody can name (a
 * screen reader says only "button"), a touch target below the size the platform asks for.
 */
function a11yOf(nodes, platform) {
  const out = [];
  const min = platform === 'ios' ? 44 : 48;
  for (const n of nodes) {
    if (n.type !== 'el' || !n.role) continue;
    if (!n.name) out.push({ id: 'control-name', impact: 'serious', help: `A ${n.role} has no accessible name`, nodes: [`${n.tag} at ${Math.round(n.box.x)},${Math.round(n.box.y)}`] });
    if (n.box.w < min - 0.5 || n.box.h < min - 0.5) out.push({ id: 'target-size', impact: 'moderate', help: `Touch target ${Math.round(n.box.w)}x${Math.round(n.box.h)} is below ${min}x${min}`, nodes: [`${n.role} "${n.name || ''}"`] });
  }
  return out;
}

module.exports = { isApp, parseXml, nodesOf, capture, scopeOf, fillUnmeasured, dropUnseen, tileOf, a11yOf, LIMITS, inkOf, bgOf };
