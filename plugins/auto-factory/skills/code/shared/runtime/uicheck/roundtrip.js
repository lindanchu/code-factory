// Is a label map right? Checked on the design alone, no web: a design page is opened in one state with the package's
// own sample data, the reader (view.js) reads the screen the way it will read the web, the map (tojson.js) turns what
// it read into data, and every key of the sample data that does not come back the same is reported. A key that never
// comes back is a key the screen does not show (an internal id): the profile has to get it another way, or the design
// must not need it.
//   const { roundtrip } = require('./roundtrip');
//   const report = await roundtrip(browser, { file, input, data, map, lists, viewport });
//   node roundtrip.js <state code> <map.json> [<list>=<path in the data> ...]      e.g. US-1 map.json keys=projects.0.keys
const fs = require('fs');
const path = require('path');
const VIEW = require('./view');
const J = require('./tojson');
const { openDesign } = require('./design');

const at = (obj, dotted) => String(dotted).split('.').filter(Boolean).reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * file: the design page; input: the props of the state; data: the sample data of the page; map: { <list>: one list of
 * the label map }; lists: { <list>: "<path of that list in the data>" } when it is not data[<list>].
 * Returns { lists: { <list>: { path, rows: { design, read }, lost: [{ key, rows, of, example }], unmapped: [keys] } },
 *           notShown: [lists whose table is not on this screen], errors: [what stopped the map], pageErrors }.
 */
async function roundtrip(browser, { file, input = {}, data, map, lists = {}, viewport = { width: 1440, height: 900 } }) {
  const ref = await openDesign(browser, file, { ...input, data }, viewport);
  let view;
  try { view = await ref.page.evaluate(VIEW); } finally { await ref.close(); }
  const report = { lists: {}, notShown: [], errors: [], pageErrors: ref.errors.slice() };
  for (const [name, m] of Object.entries(map || {})) {
    if (!m || typeof m !== 'object' || !Array.isArray(m.table) || !m.fields) continue;   // not a list of the map
    let read;
    try { read = J.rows(view, m, name); } catch (e) { report.errors.push(`${name}: ${e.message}`); continue; }
    if (!read) { report.notShown.push(name); continue; }
    if (m.nav) J.navJoin(view, m, read.rows);
    const where = lists[name] || name;
    const sample = at(data, where);
    if (!Array.isArray(sample)) { report.errors.push(`${name}: dữ liệu mẫu không có danh sách ở "${where}"`); continue; }
    const n = Math.min(sample.length, read.rows.length);
    const mapped = [...new Set(Object.keys(m.fields).concat(Object.keys((m.nav || {}).fields || {})))];
    const lost = [];
    for (const key of mapped) {
      let count = 0, example = null;
      for (let i = 0; i < n; i++) if (!same(sample[i][key], read.rows[i][key])) { count++; if (!example) example = { row: i, design: sample[i][key] === undefined ? null : sample[i][key], read: read.rows[i][key] === undefined ? null : read.rows[i][key] }; }
      if (count) lost.push({ key, rows: count, of: n, example });
    }
    const unmapped = [...new Set(sample.flatMap(r => Object.keys(r || {})))].filter(k => !mapped.includes(k));
    report.lists[name] = { path: where, rows: { design: sample.length, read: read.rows.length }, lost, unmapped };
  }
  return report;
}

module.exports = { roundtrip };

if (require.main === module) {
  (async () => {
    const [code, mapFile, ...rest] = process.argv.slice(2);
    if (!code || !mapFile) { process.stderr.write('usage: node roundtrip.js <state code> <map.json> [<list>=<path in the data> ...]\n'); process.exit(2); }
    const { load } = require('./index');
    const S = load();
    const st = S.index && (S.index.states || {})[code];
    if (!st) throw new Error(`gói design ${S.version} không có trạng thái ${code}`);
    const sampleFile = ((S.index.pages || {})[st.page] || {}).data;
    if (!sampleFile) throw new Error(`gói design ${S.version} không có dữ liệu mẫu cho trang "${st.page}"`);
    const lists = Object.fromEntries(rest.map(a => a.split('=')).filter(a => a.length === 2));
    const { chromium } = require('@playwright/test');
    const browser = await chromium.launch();
    try {
      const report = await roundtrip(browser, { file: path.join(S.pkg, st.page), input: st.input || {}, lists,
        data: JSON.parse(fs.readFileSync(path.join(S.pkg, sampleFile), 'utf8')), map: JSON.parse(fs.readFileSync(path.resolve(mapFile), 'utf8')) });
      process.stdout.write(JSON.stringify(report, null, 1) + '\n');
    } finally { await browser.close(); }
  })().catch((e) => { process.stderr.write(String((e && e.message) || e) + '\n'); process.exit(1); });
}
