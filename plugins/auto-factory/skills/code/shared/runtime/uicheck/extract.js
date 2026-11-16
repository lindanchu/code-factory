// In-page extractor of the UI check. The same code runs on the real page and on the design reference (the page of the
// design package, drawn with the same data). It lists what a user sees, without relying on class names or markup:
//   text  a text unit: the text of one inline flow (a line of a label, a paragraph, a table cell), its box and font
//   el    an element that paints something (background, border, shadow), or is a control (button, field, link, tab...),
//         or is a graphic (svg icon, image, canvas chart)
// Every node has its box in page coordinates and the nearest ancestor node as parent.
// Self-contained: it is serialised and evaluated in the page (page.evaluate(extract, opts)).
module.exports = function extract(opts) {
  opts = opts || {};
  const root = opts.rootSel ? document.querySelector(opts.rootSel) : document.body;
  if (!root) throw new Error('extract: nothing on the page matches ' + opts.rootSel);
  const norm = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  const r2 = (v) => Math.round(v * 100) / 100;
  const sx = window.scrollX, sy = window.scrollY;
  const csMap = new Map();
  const cs = (el) => { let c = csMap.get(el); if (!c) { c = getComputedStyle(el); csMap.set(el, c); } return c; };
  // "rgb(1, 2, 3)", "rgba(1, 2, 3, 0.5)", "color(srgb 0.1 0.2 0.3 / 0.5)" -> [r, g, b, a]
  const rgba = (c) => {
    const m = String(c).match(/-?[\d.]+(e-?\d+)?/g);
    if (!m) return [0, 0, 0, 0];
    const n = m.map(Number);
    if (/^color\(/.test(c)) return [Math.round(n[0] * 255), Math.round(n[1] * 255), Math.round(n[2] * 255), n.length > 3 ? n[3] : 1];
    return [Math.round(n[0]), Math.round(n[1]), Math.round(n[2]), n.length > 3 ? r2(n[3]) : 1];
  };
  const over = (top, under) => { const a = top[3]; return [0, 1, 2].map(i => Math.round(top[i] * a + under[i] * (1 - a))).concat([1]); };

  // ---- visibility: display / visibility / opacity through the ancestors, and the clip of scrolling or hidden-overflow ancestors
  const opMap = new Map();
  const opacity = (el) => { if (!el || el === document.documentElement) return 1; if (opMap.has(el)) return opMap.get(el); const o = (+cs(el).opacity) * opacity(el.parentElement); opMap.set(el, o); return o; };
  const BIG = 1e7;
  const clipMap = new Map();
  const clip = (el) => {
    if (!el || el === document.documentElement || el === document.body) return { l: -BIG, t: -BIG, r: BIG, b: BIG };
    if (clipMap.has(el)) return clipMap.get(el);
    const s = cs(el);
    // a fixed element is clipped by the viewport only
    let c = s.position === 'fixed' ? { l: -BIG, t: -BIG, r: BIG, b: BIG } : clip(el.parentElement);
    // A hidden overflow hides for good. A scrolling box does not: what it holds is reachable by scrolling, exactly like
    // content below the fold of a page that scrolls as a whole, and nothing above the scrolling box clips its content
    // (the prototype is one fixed frame with a scrolling area inside; the product scrolls the page).
    const scX = /auto|scroll/.test(s.overflowX), scY = /auto|scroll/.test(s.overflowY);
    const hx = /hidden|clip/.test(s.overflowX), hy = /hidden|clip/.test(s.overflowY);
    if (scX || scY || hx || hy) {
      const r = el.getBoundingClientRect();
      c = { l: scX ? -BIG : hx ? Math.max(c.l, r.left) : c.l, r: scX ? BIG : hx ? Math.min(c.r, r.right) : c.r, t: scY ? -BIG : hy ? Math.max(c.t, r.top) : c.t, b: scY ? BIG : hy ? Math.min(c.b, r.bottom) : c.b };
    }
    clipMap.set(el, c); return c;
  };
  // the part of a rect that can be seen through the clip of the element's ancestors, or null
  const seen = (rect, el) => {
    const c = clip(el);
    const l = Math.max(rect.left, c.l), t = Math.max(rect.top, c.t), r = Math.min(rect.right, c.r), b = Math.min(rect.bottom, c.b);
    return r - l > 0.5 && b - t > 0.5 ? { l, t, r, b } : null;
  };
  const shown = (el) => { const s = cs(el); return s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse' && opacity(el) > 0.02; };
  const box = (q) => ({ x: r2(q.l + sx), y: r2(q.t + sy), w: r2(q.r - q.l), h: r2(q.b - q.t) });

  // ---- the colour behind an element: the nearest opaque background up the tree (white when none)
  const bgMap = new Map();
  const backdrop = (el) => {
    if (!el || el === document.documentElement) { const c = rgba(cs(document.documentElement).backgroundColor); return c[3] > 0.02 ? over(c, [255, 255, 255, 1]) : [255, 255, 255, 1]; }
    if (bgMap.has(el)) return bgMap.get(el);
    const under = backdrop(el.parentElement);
    const c = rgba(cs(el).backgroundColor);
    const out = c[3] > 0.02 ? over(c, under) : under;
    bgMap.set(el, out); return out;
  };
  const same = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) <= 2;

  // ---- controls
  const roleOf = (e) => {
    // widget roles only: landmarks and structure (region, navigation, tablist, search...) are not something a user sees
    const explicit = e.getAttribute('role');
    if (explicit) { if (/^(button|link|textbox|searchbox|combobox|checkbox|radio|switch|tab|menuitem|option|slider)$/.test(explicit)) return explicit === 'searchbox' ? 'textbox' : explicit; if (!/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(e.tagName)) return null; }
    const t = e.tagName;
    if (t === 'A') return e.hasAttribute('href') ? 'link' : null;
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'DIALOG') return e.open ? 'dialog' : null;
    if (t === 'INPUT') {
      const ty = (e.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'hidden') return null;
      if (ty === 'checkbox' || ty === 'radio') return ty;
      if (ty === 'submit' || ty === 'button' || ty === 'reset') return 'button';
      return 'textbox';
    }
    return null;
  };
  const nameOf = (e, role) => {
    const al = norm(e.getAttribute('aria-label')); if (al) return al;
    const lb = e.getAttribute('aria-labelledby');
    if (lb) { const t = norm(lb.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ')); if (t) return t; }
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)) {
      const l = (e.labels && e.labels[0]) || e.closest('label');
      if (l) { const t = norm(l.innerText); if (t) return t; }
      const ph = norm(e.getAttribute('placeholder')); if (ph) return ph;
      // the design may not tie the label to the field: the short text right before the field or its wrapper (2 levels)
      for (let a = e, i = 0; a && i < 2; a = a.parentElement, i++) {
        const p = a.previousElementSibling; const t = p ? norm(p.innerText) : '';
        if (t && t.length <= 40 && !p.querySelector('input, button, a, [role=tab]')) return t;
      }
      return norm(e.getAttribute('name')) || '';
    }
    return norm(e.innerText) || norm(e.getAttribute('title')) || norm(e.getAttribute('alt')) || '';
  };
  const stateOf = (e, role) => {
    const s = {};
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') s.disabled = true;
    if (role === 'checkbox' || role === 'radio' || role === 'switch') s.checked = !!(e.checked || e.getAttribute('aria-checked') === 'true');
    if (e.getAttribute('aria-selected') === 'true' || e.getAttribute('aria-current')) s.selected = true;
    if (e.getAttribute('aria-expanded')) s.expanded = e.getAttribute('aria-expanded') === 'true';
    if (e.getAttribute('aria-pressed')) s.pressed = e.getAttribute('aria-pressed') === 'true';
    if (e.readOnly) s.readonly = true;
    if (document.activeElement === e) s.focused = true;
    if (role === 'textbox' || role === 'combobox') {
      const secret = (e.getAttribute('type') || '').toLowerCase() === 'password';
      const v = e.value == null ? '' : String(e.value);
      s.value = secret ? (v ? `(${v.length} ký tự ẩn)` : '') : v.slice(0, 120);
      if (e.getAttribute('placeholder')) {
        s.placeholder = norm(e.getAttribute('placeholder'));
        if (!v) {
          s.placeholderColor = rgba(getComputedStyle(e, '::placeholder').color);
          // a page that sets no colour for its placeholders leaves it to the browser (#757575 in one, a share of the
          // text colour in another): a probe field with the same text colour, in a shadow root the page's rules do
          // not reach, draws the browser's own; the same colour means the page set none
          try {
            const host = document.createElement('div');
            host.style.cssText = 'position:absolute;left:-9999px;top:0';
            document.body.appendChild(host);
            const probe = document.createElement(e.tagName === 'TEXTAREA' ? 'textarea' : 'input');
            probe.setAttribute('placeholder', 'x');
            probe.style.color = cs(e).color;
            host.attachShadow({ mode: 'open' }).appendChild(probe);
            if (String(rgba(getComputedStyle(probe, '::placeholder').color)) === String(s.placeholderColor)) s.placeholderDefault = true;
            host.remove();
          } catch (err) { /* a page that refuses the probe keeps the comparison */ }
        }
      }
      const cs0 = cs(e); const er = e.getBoundingClientRect();
      s.textX = r2(er.left + parseFloat(cs0.paddingLeft) + parseFloat(cs0.borderLeftWidth) + sx);
    }
    return s;
  };

  // ---- paint of an element: what it draws itself
  // box-shadow list -> { inner: ring drawn inside (same look as a border), outer: rings outside (focus ring), rest: real shadows }
  const shadowsOf = (v) => {
    const out = { inner: null, outer: [], rest: [] };
    if (!v || v === 'none') return out;
    for (const part of v.split(/,(?![^(]*\))/)) {
      const col = (part.match(/(rgba?\([^)]*\)|color\([^)]*\)|#[0-9a-f]+)/i) || [])[0];
      const nums = part.replace(col || '', '').match(/-?[\d.]+px/g) || [];
      const [x, y, blur, spread] = [0, 1, 2, 3].map(i => parseFloat(nums[i] || 0));
      const c = rgba(col || 'rgb(0,0,0)');
      if (c[3] <= 0.02) continue;
      const inset = /inset/.test(part);
      if (!x && !y && !blur && spread > 0) { if (inset) { if (!out.inner) out.inner = { w: spread, c }; } else out.outer.push({ w: spread, c }); }
      else out.rest.push(`${inset ? 'inset ' : ''}${x} ${y} ${blur} ${spread} ${c.join(',')}`);
    }
    return out;
  };
  const paintOf = (el, s) => {
    const bg = rgba(s.backgroundColor);
    const img = s.backgroundImage && s.backgroundImage !== 'none' ? s.backgroundImage.replace(/\s+/g, ' ').slice(0, 80) : '';
    const bw = [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth, s.borderLeftWidth].map(parseFloat);
    const bst = [s.borderTopStyle, s.borderRightStyle, s.borderBottomStyle, s.borderLeftStyle];
    const bc = [s.borderTopColor, s.borderRightColor, s.borderBottomColor, s.borderLeftColor].map(rgba);
    let sides = bw.map((w, i) => (w > 0 && bst[i] !== 'none' && bst[i] !== 'hidden' && bc[i][3] > 0.02 ? { w, c: bc[i] } : null));
    const sh = shadowsOf(s.boxShadow);
    // a ring drawn with an inset shadow looks like a border: one representation for both
    if (sh.inner && !sides.some(Boolean)) sides = [0, 1, 2, 3].map(() => ({ w: sh.inner.w, c: sh.inner.c }));
    else if (sh.inner) sh.rest.push(`inset ring ${sh.inner.w} ${sh.inner.c.join(',')}`);
    const rings = sh.outer.slice();
    if (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0 && rgba(s.outlineColor)[3] > 0.02) rings.push({ w: parseFloat(s.outlineWidth), c: rgba(s.outlineColor) });
    const under = backdrop(el.parentElement);
    const fill = bg[3] > 0.02 && !same(over(bg, under), under);
    if (!fill && !img && !sides.some(Boolean) && !sh.rest.length && !rings.length) return null;
    return {
      bg: fill ? bg : null, bgImage: img || undefined,
      border: sides.some(Boolean) ? sides : undefined,
      radius: [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius].map(v => parseFloat(v) || 0),
      shadow: sh.rest.length ? sh.rest.join(' | ') : undefined, outline: rings.length ? rings.map(q => `${q.w}px ${q.c.join(',')}`).join(' + ') : undefined,
    };
  };
  const graphicOf = (el) => {
    const t = el.tagName.toLowerCase();
    if (t === 'svg') {
      if (el.parentElement && el.parentElement.closest('svg')) return null;
      const use = el.querySelector('use');
      const href = use && (use.getAttribute('href') || use.getAttribute('xlink:href') || '');
      const name = (href && href.split('#').pop()) || el.getAttribute('data-icon') || (el.closest('[data-icon]') || el).getAttribute('data-icon') || '';
      return { kind: 'svg', name: name.replace(/^(ic|i|icon)[-_]/, ''), color: rgba(cs(el).color) };
    }
    if (t === 'img') return { kind: 'img', natural: `${el.naturalWidth}x${el.naturalHeight}` };
    if (t === 'canvas') return { kind: 'canvas' };
    if (t === 'video') return { kind: 'video' };
    return null;
  };

  // The box a user sees for a field: the field when it paints its own border or background, else the nearest ancestor
  // (up to 3 levels) that does. Design and product often differ here (the ring on the input vs on a wrapper).
  const fieldBox = new Map();   // box element -> field element
  const fieldSkip = new Set();  // fields represented by their box element
  for (const f of root.querySelectorAll('input, textarea, select')) {
    const ty = (f.getAttribute('type') || '').toLowerCase();
    if (ty === 'hidden' || ty === 'checkbox' || ty === 'radio' || !shown(f)) continue;
    const fr = f.getBoundingClientRect();
    if (fr.width < 0.5 || fr.height < 0.5 || paintOf(f, cs(f))) continue;
    for (let a = f.parentElement, i = 0; a && a !== root && i < 3; a = a.parentElement, i++) {
      const ar = a.getBoundingClientRect();
      if (ar.height > fr.height + 48) break;
      if (paintOf(a, cs(a))) { if (!fieldBox.has(a)) { fieldBox.set(a, f); fieldSkip.add(f); } break; }
    }
  }

  // A tick box or radio that the page hides and redraws with a styled element: the drawn element (the next painted
  // sibling, else the label) carries the role and the state, so "ticked" is compared as a state on both sides.
  const proxy = new Map();   // drawn element -> native input
  for (const f of root.querySelectorAll('input[type=checkbox], input[type=radio]')) {
    const fr = f.getBoundingClientRect(); const fs0 = cs(f);
    const hiddenNative = fr.width < 2 || fr.height < 2 || +fs0.opacity < 0.05 || fs0.appearance === 'none' && !paintOf(f, fs0) || /rect\(0|inset\(50%\)/.test(fs0.clip + fs0.clipPath);
    if (!hiddenNative) continue;
    let drawn = null;
    for (let sib = f.nextElementSibling; sib && !drawn; sib = sib.nextElementSibling) if (shown(sib) && sib.getBoundingClientRect().width > 0 && paintOf(sib, cs(sib))) drawn = sib;
    if (!drawn) { const l = f.closest('label'); if (l) drawn = [...l.querySelectorAll('*')].find(e => e !== f && shown(e) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().width <= 28 && paintOf(e, cs(e))) || null; }
    if (drawn && !proxy.has(drawn)) proxy.set(drawn, f);
  }

  const nodes = [];
  const idOf = new Map();
  const parentId = (el) => { for (let a = el; a && a !== root.parentElement; a = a.parentElement) if (idOf.has(a)) return idOf.get(a); return 0; };

  // ---- pass 1: elements (document order, so a parent gets its id before its children)
  const all = [root, ...root.querySelectorAll('*')];
  let order = 0;
  for (const el of all) {
    order++;
    const tag = el.tagName;
    if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD|META|LINK|TITLE|OPTION|OPTGROUP|BR|WBR)$/.test(tag)) continue;
    if (el.closest('svg') && tag.toLowerCase() !== 'svg') continue;
    if (!shown(el)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 0.5 || rect.height < 0.5) continue;
    const vis = seen(rect, el.parentElement);
    if (!vis) continue;
    if (fieldSkip.has(el)) continue;
    const s = cs(el);
    const field = fieldBox.get(el) || proxy.get(el);
    const role = field ? roleOf(field) : roleOf(el);
    const graphic = graphicOf(el);
    // the design file's own page background (the designer's canvas) is not part of the product
    const paint = el === root && opts.skipRootPaint ? null : paintOf(el, s);
    if (!role && !graphic && !paint) continue;
    const de0 = document.documentElement;
    const pageBox = el === root && el === document.body ? { x: 0, y: 0, w: de0.clientWidth, h: Math.max(de0.scrollHeight, de0.clientHeight) } : null;
    const n = { id: nodes.length + 1, type: 'el', tag: tag.toLowerCase(), order, parent: el === root ? 0 : parentId(el.parentElement), box: pageBox || box(vis),
      full: (rect.width - (vis.r - vis.l) > 1 || rect.height - (vis.b - vis.t) > 1) ? { x: r2(rect.left + sx), y: r2(rect.top + sy), w: r2(rect.width), h: r2(rect.height) } : undefined,
      opacity: opacity(el) < 0.99 ? r2(opacity(el)) : undefined };
    if (role) { n.role = role; n.name = nameOf(field || el, role).slice(0, 100); n.state = stateOf(field || el, role); }
    if (graphic) n.graphic = graphic;
    if (paint) n.paint = paint;
    if (paint) n.under = backdrop(el.parentElement);
    if (s.position === 'fixed' || s.position === 'sticky') n.pos = s.position;
    if (el === root || (rect.width >= window.innerWidth - 1 && rect.height >= window.innerHeight - 1)) n.page = true;
    idOf.set(el, n.id);
    nodes.push(n);
  }

  // ---- pass 2: text units. A unit = the text nodes that share the nearest ancestor which is not inline.
  const units = new Map();
  const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let ti = 0;
  for (let t = tw.nextNode(); t; t = tw.nextNode()) {
    ti++;
    if (!t.nodeValue || !t.nodeValue.trim()) continue;
    const pe = t.parentElement;
    if (!pe || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE|OPTION|TEXTAREA)$/.test(pe.tagName)) continue;
    const svg = pe.closest('svg');
    if (svg) {
      // a chart: each <text> is one text node, coloured by its fill
      const top = svg.parentElement && svg.parentElement.closest('svg') ? null : svg;
      if (!top || top.getBoundingClientRect().width <= 100 || !/^(text|tspan)$/i.test(pe.tagName) || !shown(pe)) continue;
      const r0 = pe.getBoundingClientRect(); if (r0.width < 0.5 || r0.height < 0.5) continue;
      // what an svg clip-path cuts off is not seen (the pages of a scrolling legend)
      let cut = false;
      for (let a = pe; a && a !== top; a = a.parentElement) {
        const m0 = ((a.getAttribute('clip-path') || '') + ' ' + (cs(a).clipPath || '')).match(/#([^)"'\s]+)/);
        const shape = m0 && document.getElementById(m0[1]) && document.getElementById(m0[1]).firstElementChild;
        const M = shape && shape.getBBox && a.getScreenCTM && a.getScreenCTM();
        if (!M) continue;
        const bb = shape.getBBox(); const p1 = new DOMPoint(bb.x, bb.y).matrixTransform(M), p2 = new DOMPoint(bb.x + bb.width, bb.y + bb.height).matrixTransform(M);
        const vis = Math.max(0, Math.min(r0.right, Math.max(p1.x, p2.x)) - Math.max(r0.left, Math.min(p1.x, p2.x))) * Math.max(0, Math.min(r0.bottom, Math.max(p1.y, p2.y)) - Math.max(r0.top, Math.min(p1.y, p2.y)));
        if (vis < 0.5 * r0.width * r0.height) { cut = true; break; }
      }
      if (cut) continue;
      const s0 = cs(pe);
      nodes.push({ id: nodes.length + 1, type: 'text', order: 100000 + ti, parent: idOf.get(top) || parentId(top), box: box({ l: r0.left, t: r0.top, r: r0.right, b: r0.bottom }), text: norm(t.nodeValue).slice(0, 300), lines: 1, chart: true,
        font: { size: parseFloat(s0.fontSize), weight: s0.fontWeight, family: s0.fontFamily.split(',')[0].replace(/["']/g, '').trim(), lh: 'normal' }, color: rgba(s0.fill), under: backdrop(top) });
      continue;
    }
    if (!shown(pe)) continue;
    const range = document.createRange(); range.selectNodeContents(t);
    const rects = [...range.getClientRects()].map(r => seen(r, pe)).filter(Boolean);
    if (!rects.length) continue;
    let u = pe;
    while (u !== root && /^(inline|contents)$/.test(cs(u).display)) u = u.parentElement;
    let unit = units.get(u);
    if (!unit) { unit = { el: u, runs: [], rects: [], order: 100000 + ti }; units.set(u, unit); }
    const s = cs(pe);
    let text = t.nodeValue.replace(/[\s ]+/g, ' ');
    if (s.textTransform === 'uppercase') text = text.toUpperCase(); else if (s.textTransform === 'lowercase') text = text.toLowerCase();
    else if (s.textTransform === 'capitalize') text = text.replace(/(^|\s)(\S)/g, (m, a, b) => a + b.toUpperCase());
    unit.runs.push({ text, len: text.trim().length, font: { size: parseFloat(s.fontSize), weight: s.fontWeight, family: s.fontFamily.split(',')[0].replace(/["']/g, '').trim(), style: s.fontStyle !== 'normal' ? s.fontStyle : undefined,
      lh: s.lineHeight, ls: s.letterSpacing !== 'normal' && parseFloat(s.letterSpacing) ? s.letterSpacing : undefined, deco: s.textDecorationLine !== 'none' ? s.textDecorationLine : undefined }, color: rgba(s.color), under: backdrop(pe) });
    unit.rects.push(...rects);
  }
  for (const unit of units.values()) {
    const text = norm(unit.runs.map(r => r.text).join(''));
    if (!text) continue;
    const l = Math.min(...unit.rects.map(r => r.l)), t = Math.min(...unit.rects.map(r => r.t)), r = Math.max(...unit.rects.map(r => r.r)), b = Math.max(...unit.rects.map(r => r.b));
    // lines: rects whose vertical centres differ by more than half a rect height start a new line
    const cy = unit.rects.map(q => (q.t + q.b) / 2).sort((a, b) => a - b);
    let lines = 1; for (let i = 1; i < cy.length; i++) if (cy[i] - cy[i - 1] > (unit.rects[0].b - unit.rects[0].t) * 0.6) lines++;
    const main = unit.runs.slice().sort((a, b) => b.len - a.len)[0];
    const us = cs(unit.el);
    const cut = /hidden|clip/.test(us.overflowX) && unit.el.scrollWidth > unit.el.clientWidth + 1;
    const n = { id: nodes.length + 1, type: 'text', order: unit.order, parent: parentId(unit.el), box: box({ l, t, r, b }), text: text.slice(0, 300), lines,
      font: main.font, color: main.color, under: main.under,
      mixed: unit.runs.length > 1 && unit.runs.some(q => q.len && (q.font.weight !== main.font.weight || q.font.size !== main.font.size || q.color.join() !== main.color.join()))
        ? unit.runs.filter(q => q.len).map(q => ({ text: q.text.trim().slice(0, 60), size: q.font.size, weight: q.font.weight, family: q.font.family, color: q.color })) : undefined,
      cut: cut ? (us.textOverflow === 'ellipsis' ? 'ellipsis' : 'clipped') : undefined, align: us.textAlign };
    nodes.push(n);
  }
  const de = document.documentElement;
  return { nodes, viewport: { w: window.innerWidth, h: window.innerHeight }, doc: { w: Math.max(de.scrollWidth, document.body.scrollWidth), h: Math.max(de.scrollHeight, document.body.scrollHeight), clientW: de.clientWidth }, scroll: { x: sx, y: sy },
    active: document.activeElement && document.activeElement !== document.body ? (document.activeElement.tagName.toLowerCase()) : null };
};
