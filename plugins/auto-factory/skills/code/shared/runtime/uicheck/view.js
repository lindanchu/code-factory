// A reader that knows no CSS class and nothing of the product's code: it reads what a person sees on the page and what
// assistive technology is told (roles, labels, titles). The same reader runs on the real page and on the design page.
//   tables  every group of rows lined up in columns under a row of labels: the header labels, and for each row the
//           visible pieces of text under each header (found by position: the design page has no role or label for it)
//   navs    every navigation list, named by its label or by the short title written above it: its items, each with its
//           pieces grouped as they are laid out
//   side    the side panel: the pieces outside any navigation list, and the pieces of its account button
//   tabs    every row of tabs, each tab with its pieces (a count is a piece) and whether it is the selected one
//   h1      the main headings
// A piece is { text, title } (title = the tooltip text of the piece or of what holds it); a piece inside a button says so.
module.exports = function VIEW() {
  const norm = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  const vis = (e) => { const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden') return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const kids = (e) => [...e.children].filter(vis);
  const own = (e) => norm([...e.childNodes].filter(n => n.nodeType === 3).map(n => n.nodeValue).join(' '));
  const up = (e, root, f) => { for (let x = e; x && root.contains(x); x = x.parentElement) { const v = f(x); if (v) return v; } return ''; };
  const isBtn = (x) => x.tagName === 'BUTTON' || ['button', 'menuitem'].includes(x.getAttribute('role'));
  const pieces = (root, skip) => {
    const out = [];
    const walk = (e) => {
      if (!vis(e) || e.tagName.toLowerCase() === 'svg' || (skip && e !== root && e.matches(skip))) return;
      const t = own(e);
      if (t) { const p = { text: t }; const ti = up(e, root, x => x.getAttribute('title')); if (ti) p.title = ti; if (up(e, root, x => (isBtn(x) ? '1' : ''))) p.button = true; out.push(p); }
      else if (isBtn(e) && e.getAttribute('aria-label') && !norm(e.innerText)) out.push({ text: '', label: e.getAttribute('aria-label'), button: true });
      for (const c of e.children) walk(c);
    };
    walk(root); return out;
  };
  // ---- tables, found by what is seen (the design page gives no role and no label for them): a group of rows whose parts
  //      sit side by side and line up in columns; the first row holds only labels (the header)
  const rect = (e) => e.getBoundingClientRect();
  const cellsOf = (e) => { const w = rect(e).width; const k = kids(e).map(c => ({ c, b: rect(c) })).filter(x => x.b.width < w * 0.9).sort((x, y) => x.b.left - y.b.left);
    return k.length >= 2 && k.every((x, i) => i === 0 || (x.b.left >= k[i - 1].b.right - 1 && x.b.top < k[i - 1].b.bottom && k[i - 1].b.top < x.b.bottom)) ? k : null; };
  // a row, or a wrapper around exactly one row (a link around the cells)
  const rowOf = (e) => { for (let x = e, d = 0; x && d < 3; d++) { if (cellsOf(x)) return x; const k = kids(x); if (k.length !== 1) return null; x = k[0]; } return null; };
  const tables = [];
  const seen = new Set();
  for (const G of document.querySelectorAll('body *')) {
    if (!vis(G) || G.tagName === 'TR' || G.closest('svg, nav, [role=navigation]')) continue;
    let rowEls = G.tagName === 'TABLE' ? [...G.querySelectorAll('tr')].filter(vis) : kids(G);
    if (rowEls.length < 2) continue;
    const head = rowOf(rowEls[0]); if (!head || seen.has(head)) continue;
    const hc = cellsOf(head); if (hc.some(k => k.c.querySelector('button, a, input, select'))) continue;
    const cols = hc.map(k => ({ label: norm(k.c.textContent), x: k.b.left, r: k.b.right })).filter(c => c.label);
    if (cols.length < 2) continue;
    const body = [];
    for (const el of rowEls.slice(1)) {
      const R = rowOf(el); if (!R) continue;
      const cells = {}; let aligned = 0;
      for (const k of cellsOf(R)) {
        const cx = (k.b.left + k.b.right) / 2;
        const col = cols.find(c => Math.abs(c.x - k.b.left) <= 2) || cols.find(c => cx >= c.x && cx < c.r);
        if (col && Math.abs(col.x - k.b.left) <= 2) aligned++;
        const key = col ? col.label : '';
        (cells[key] = cells[key] || []).push(...pieces(k.c));
      }
      if (aligned < 2) continue;
      const a = el.closest('a[href]') || el.querySelector('a[href]');
      body.push({ href: a ? a.getAttribute('href') : '', cells });
    }
    if (!body.length) continue;
    seen.add(head);
    tables.push({ head: cols.map(c => c.label), rows: body });
  }
  // ---- navigation lists; the label is the one told to assistive technology, else the short title written just above the list
  const titleAbove = (n) => { const p = n.previousElementSibling; const t = p && vis(p) ? norm(p.textContent) : ''; return t.length <= 40 ? t : ''; };   // the text as written, not as a style upper-cases it
  const navs = [...document.querySelectorAll('nav, [role=navigation]')].filter(vis).map(n => ({
    label: n.getAttribute('aria-label') || titleAbove(n),
    items: [...n.querySelectorAll('a, button')].filter(vis).map(a => ({ href: a.getAttribute('href') || '', current: a.getAttribute('aria-current') || '', parts: kids(a).map(c => pieces(c).map(p => ({ text: p.text, ...(p.title ? { title: p.title } : {}) }))).filter(p => p.length) })),
  }));
  // ---- the side panel
  const aside = [...document.querySelectorAll('aside, [role=complementary]')].find(vis);
  let side = null;
  if (aside) {
    const lastNav = [...aside.querySelectorAll('nav, [role=navigation]')].filter(vis).pop();
    // the account button: the last button of the panel that belongs to no list and no menu
    const acct = [...aside.querySelectorAll('button')].filter(b => vis(b) && !b.closest('nav, [role=navigation], [role=menu]')).pop();
    const loose = [];
    const walk = (e) => {
      if (!vis(e) || e.tagName.toLowerCase() === 'svg' || e.matches('nav, [role=navigation], [role=menu], button, form')) return;
      const t = own(e); if (t) loose.push({ text: t, afterNavs: !!(lastNav && (lastNav.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)) });
      for (const c of e.children) walk(c);
    };
    walk(aside);
    side = { loose, account: acct ? pieces(acct).map(p => p.text) : [], accountOpen: acct ? acct.getAttribute('aria-expanded') === 'true' : false };
  }
  const tabs = [...document.querySelectorAll('[role=tablist]')].filter(vis).map(t => [...t.querySelectorAll('[role=tab]')].filter(vis).map(x => ({ pieces: pieces(x).map(p => p.text), selected: x.getAttribute('aria-selected') === 'true' })));
  const h1 = [...document.querySelectorAll('h1')].filter(vis).map(h => norm(h.innerText));
  return { tables, navs, side, tabs, h1 };
};
