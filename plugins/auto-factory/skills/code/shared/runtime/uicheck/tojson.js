// From what the reader saw (view.js) to the design's data, driven by a map the project's profile declares: which visible
// label holds which key. Nothing here knows the product: a table is found by its header labels, a value by its column.
// A label the map does not know stops with a sentence a person can act on; a table that is not on the page gives null.
//
// One list of the map:
//   { table:  ["<header label>", ...],                 the labels that name the table, in the order they are shown
//     fields: { <key>: { col: "<header label>",        the column the value is read from
//                        piece: 1,                     which piece of text of the cell (default the first)
//                        attr: "title",                the tooltip of the piece instead of its text
//                        strip: " (bạn)",              a suffix the screen adds to the value
//                        labels: { "Hoạt động": "active" },   shown label -> value of the data (keepOther: other
//                                                      texts are kept as they are; else an unknown label stops)
//                        number: true, optional: true, default: "",
//                        list: true, skip: ["Chưa gán"], more: "^\\+\\d+$" }     every piece of the cell; a "+3" piece
//                                                      stands for the names in its tooltip
//               <key>: { value: 0 } },                 a key the screen does not show
//     shown:  { <name>: { col, number } | { href: true } },   what a row shows that is not a key of the data
//     mark:   { <name>: { col, endsWith: " (bạn)" } },  -> marks[name] = id of the row whose piece ends so
//     nav:    { label: "<label of a navigation list>", match: "<key>",
//               fields: { <key>: { part: [i, j], attr: "title", default: "" } },
//               translate: [{ when: { <key>: "<regex>" }, set: { <key>: <value> } }] } }
const pieceOf = (ps, spec) => (ps || []).filter(p => !p.button)[spec.piece || 0];
function value(ps, spec, where) {
  if ('value' in spec) return spec.value;
  ps = (ps || []).filter(p => !p.button);
  if (spec.list) {
    const out = [];
    for (const p of ps) { if ((spec.skip || []).includes(p.text)) continue; if (spec.more && new RegExp(spec.more).test(p.text)) { out.push(...(p.title || '').split(/,\s*/).filter(Boolean)); continue; } out.push(p.text); }
    return out;
  }
  const p = ps[spec.piece || 0];
  if (!p) return spec.optional ? undefined : (spec.default !== undefined ? spec.default : '');
  let v = spec.attr === 'title' ? (p.title || (spec.default !== undefined ? spec.default : '')) : p.text;
  if (typeof v === 'string' && spec.strip && v.endsWith(spec.strip)) v = v.slice(0, -spec.strip.length);
  if (spec.labels) { if (Object.prototype.hasOwnProperty.call(spec.labels, v)) v = spec.labels[v]; else if (!spec.keepOther) throw new Error(`nhãn "${v}" ở ${where} chưa có trong bảng nhãn của bảng khai (${Object.keys(spec.labels).join(', ')})`); }
  if (spec.number) v = Number(v);
  return v;
}
/** The table of the view whose header holds every label the map names, in that order. */
function tableOf(view, m) {
  const need = m.table;
  const has = (head) => { let i = -1; return need.every(l => { const k = head.indexOf(l, i + 1); if (k < 0) return false; i = k; return true; }); };
  return view.tables.find(t => has(t.head)) || null;
}
/** Rows of one list of the map, read from the view. Returns null when the page does not show that table. */
function rows(view, m, name) {
  const t = tableOf(view, m); if (!t) return null;
  const out = [], shown = [], marks = {};
  for (const r of t.rows) {
    const o = {};
    for (const [k, spec] of Object.entries(m.fields)) { const v = value(spec.col !== undefined ? r.cells[spec.col] : null, spec, `cột "${spec.col}" của bảng ${name}`); if (v !== undefined) o[k] = v; }
    const s = {}; for (const [k, spec] of Object.entries(m.shown || {})) s[k] = spec.href ? r.href : value(r.cells[spec.col], spec, `cột "${spec.col}"`);
    for (const [k, rule] of Object.entries(m.mark || {})) { const p = pieceOf(r.cells[rule.col], rule); if (p && rule.endsWith && p.text.endsWith(rule.endsWith)) marks[k] = o.id; }
    out.push(o); shown.push(s);
  }
  return { rows: out, shown, marks, head: t.head };
}
/** What the labelled navigation list says about the rows of a list (the side panel's project list): joined by one field. */
function navJoin(view, m, list) {
  const nav = view.navs.find(n => n.label === m.nav.label); if (!nav) return;
  const items = nav.items.map(it => { const o = {}; for (const [k, spec] of Object.entries(m.nav.fields)) { const p = ((it.parts[spec.part[0]] || [])[spec.part[1]]); o[k] = p ? (spec.attr === 'title' ? (p.title || p.text) : p.text) : (spec.default !== undefined ? spec.default : ''); } return o; });
  for (const row of list) {
    const it = items.find(x => x[m.nav.match] === row[m.nav.match]) || {};
    for (const k of Object.keys(m.nav.fields)) if (k !== m.nav.match) row[k] = it[k] !== undefined ? it[k] : (m.nav.fields[k].default !== undefined ? m.nav.fields[k].default : '');
    for (const t of m.nav.translate || []) if (Object.entries(t.when).every(([k, re]) => new RegExp(re).test(row[k] || ''))) Object.assign(row, t.set);
  }
}
const footer = (view) => (view.side ? view.side.loose.filter(p => p.afterNavs).map(p => p.text) : []);
module.exports = { rows, navJoin, footer, tableOf, value };
