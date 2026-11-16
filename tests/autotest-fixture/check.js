// Checks what a run of the fixture left behind. Called by run.sh:
//   node check.js main     <fixture root> <results dir>   the whole suite; conventions: medium, from 4px, BAD fails the step
//   node check.js column   <fixture root> <results dir>   WEB-DEM-03 alone, a BAD screen does not fail the case
//   node check.js unchosen <fixture root> <results dir>   WEB-DEM-03 alone, no threshold chosen: nothing is judged
// Standard library only. Exits 1 with the first thing that is not as expected.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const [mode, ROOT, DIR] = [process.argv[2], path.resolve(process.argv[3] || ''), path.resolve(process.argv[4] || '')];
const PROJECT = 'chromium-1440';
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const NOT_CHOSEN = 'chưa chọn ngưỡng lệch';
const TYPES = ['BACKGROUND_COLOR', 'FONT', 'SPACING', 'MISSING_ELEMENT', 'SIZE', 'RADIUS'];

const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const stampOf = (root) => execFileSync(process.execPath, [path.join(root, 'web', 'autotest', 'uicheck', 'index.js'), '--stamp'],
  { cwd: path.join(root, 'web'), encoding: 'utf8', env: { ...process.env, NODE_PATH: '' } }).trim();   // as the tool asks: no test library needed

// ---------------------------------------------------------------- reading a results folder
function load() {
  const cases = {};
  for (const name of fs.readdirSync(path.join(DIR, 'cases')).sort()) {
    assert.ok(name.endsWith('.json'), `cases/ holds only case files, found ${name}`);
    const raw = fs.readFileSync(path.join(DIR, 'cases', name), 'utf8');
    const c = JSON.parse(raw);
    assert.ok(/^[0-9a-f]+-[0-9a-f]+-chromium-1440\.json$/.test(name), `case file named <test id>-<project>.json: ${name}`);
    assert.strictEqual(c.projectName, PROJECT);
    assert.ok(!/"body"\s*:/.test(raw), `${name}: no attachment is kept inline`);
    c._id = name.slice(0, -`-${PROJECT}.json`.length);
    cases[c.title.split(' ')[0]] = c;
  }
  return cases;
}
const attachment = (r, name) => r.attachments.filter(a => a.name === name);
const one = (r, name) => { const a = attachment(r, name); assert.strictEqual(a.length, 1, `one attachment "${name}" (attempt ${r.retry}), found ${a.length}`); return a[0]; };
const none = (r, name) => assert.strictEqual(attachment(r, name).length, 0, `no attachment "${name}" (attempt ${r.retry})`);
const bytes = (a) => { assert.ok(a.path && !path.isAbsolute(a.path), `attachment ${a.name} has a path relative to the results folder: ${a.path}`); return fs.readFileSync(path.join(DIR, a.path)); };
const json = (r, name) => JSON.parse(bytes(one(r, name)).toString('utf8'));
const text = (r, name) => bytes(one(r, name)).toString('utf8');

/** Every attachment of every attempt: a file under the results folder; one given as a body sits in attachments/. */
function checkFiles(c) {
  for (const r of c.results) r.attachments.forEach((a, n) => {
    assert.deepStrictEqual(Object.keys(a).sort(), ['contentType', 'name', 'path'], `${c.title}: attachment ${a.name} is { name, contentType, path }`);
    const b = bytes(a);
    assert.ok(b.length > 0, `${a.name} is not empty`);
    if (a.contentType === 'image/png') assert.ok(b.subarray(0, 8).equals(PNG), `${a.name} is a PNG`);
    if (a.name === 'video' || a.name === 'error-context') assert.ok(a.path.startsWith('artifacts/'), `${a.name} keeps its own file: ${a.path}`);
    else assert.strictEqual(a.path, `attachments/${c._id}-${r.retry}-${n}-${a.name}`, 'an attachment given as a body is written by test id, attempt, position and name');
  });
}
/** The UI attachments of one attempt agree with each other and with the contract of the tool. */
function checkUi(c, r, expected, stamp) {
  const states = json(r, 'ui-states');
  const info = json(r, 'ui-info');
  assert.deepStrictEqual(Object.keys(info).sort(), ['limits', 'package', 'package_sha', 'parts', 'stamp', 'viewport']);
  assert.strictEqual(info.package, 'v0.1');
  assert.strictEqual(info.package_sha, readJson(path.join(ROOT, 'design-src', 'v0.1', 'autotest-states.json')).sha256);
  assert.deepStrictEqual(info.viewport, { width: 1440, height: 900 });
  assert.strictEqual(info.stamp, stamp, `${c.title}: ui-info.stamp is the current stamp`);
  assert.strictEqual(info.limits.length, 3);
  const checks = attachment(r, 'ui_checks').length ? json(r, 'ui_checks') : [];
  assert.deepStrictEqual(checks.map(k => [k.name, k.step, k.level]), expected.sent, `${c.title}: the screens sent`);
  assert.deepStrictEqual(states.map(s => [s.screen, s.step, s.code, s.scope || null, s.level, s.sent]), expected.states, `${c.title}: every check made`);
  assert.deepStrictEqual(Object.keys(info.parts).sort(), checks.map(k => k.screenshot).sort(), 'one sentence per screen sent');
  const steps = new Set();
  for (const k of checks) {
    assert.deepStrictEqual(Object.keys(k).sort(), ['design_screenshot', 'design_source', 'diff_percent', 'diffs', 'level', 'name', 'screenshot', 'step']);
    assert.ok(!steps.has(k.step), 'at most one screen per step'); steps.add(k.step);
    assert.strictEqual(k.design_source, 'REFERENCE');
    assert.ok(/^ui-\d+$/.test(k.screenshot) && k.design_screenshot === k.screenshot.replace('ui-', 'design-'));
    assert.ok(bytes(one(r, k.screenshot)).length > 1000 && bytes(one(r, k.design_screenshot)).length > 1000, 'both images of a screen are attached');
    assert.ok(k.diffs.length <= 50 && k.diff_percent >= 0 && k.diff_percent <= 100);
    assert.strictEqual(k.level, k.diffs.some(d => d.level === 'BAD') ? 'BAD' : k.diffs.length ? 'MINOR' : 'OK', 'the level is the worst difference');
    for (const d of k.diffs) {
      assert.deepStrictEqual(Object.keys(d).sort(), ['actual', 'box', 'design', 'element', 'level', 'type']);
      assert.ok(TYPES.includes(d.type) && ['MINOR', 'BAD'].includes(d.level));
      for (const v of ['x', 'y', 'w', 'h']) assert.ok(d.box[v] >= 0 && d.box[v] <= 100, 'a box is in percent of the screen');
      assert.ok(!/[.#][a-z]+-[a-z]|class=|querySelector/.test(d.element + d.design.replace(/\(--[a-z0-9-]+( …)?\)/g, '') + d.actual.replace(/\(--[a-z0-9-]+( …)?\)/g, '')), `plain words, no selector: ${JSON.stringify(d)}`);
    }
    assert.ok(info.parts[k.screenshot].length > 10);
  }
  // only the images of the screens sent are attached
  const images = r.attachments.filter(a => /^(ui|design)-\d+$/.test(a.name)).map(a => a.name).sort();
  assert.deepStrictEqual(images, checks.flatMap(k => [k.screenshot, k.design_screenshot]).sort());
  for (const s of states) if (s.level === null) assert.ok(s.sent === false && typeof s.why === 'string' && s.why.length > 5, 'a screen not compared says why');
  return { checks, states, info, skipped: attachment(r, 'ui-bo-qua').length ? text(r, 'ui-bo-qua').trim().split('\n') : [] };
}

// ---------------------------------------------------------------- the whole suite
function main() {
  const run = readJson(path.join(DIR, 'run.json'));
  assert.deepStrictEqual(Object.keys(run), ['started', 'finished', 'status', 'tests', 'done', 'projects']);
  assert.ok(!Number.isNaN(Date.parse(run.started)) && !Number.isNaN(Date.parse(run.finished)), 'run.json has the start and the end');
  assert.deepStrictEqual([run.status, run.tests, run.done, run.projects], ['failed', 8, 8, [PROJECT]]);

  const cases = load();
  assert.deepStrictEqual(Object.keys(cases).sort(), ['WEB-DEM-01', 'WEB-DEM-02', 'WEB-DEM-03', 'WEB-DEM-04', 'WEB-DEM-05', 'WEB-DEM-06', 'WEB-DEM-07-ENV', 'WEB-DEM-08']);
  const plans = { ...readJson(path.join(ROOT, 'web', 'plans', 'DEM.json')), ...readJson(path.join(ROOT, 'web', 'plans', 'DEM.p2.json')) };
  const statuses = {};
  for (const [code, c] of Object.entries(cases)) {
    assert.deepStrictEqual(Object.keys(c).filter(k => k !== '_id'), ['title', 'file', 'projectName', 'annotations', 'results']);
    assert.strictEqual(c.title, `${code} ${plans[code].name}`, 'the title comes from the plan (never from DEM.matrix.json)');
    assert.strictEqual(c.file, `tests/dem/${/0[67]/.test(code) ? 'more' : 'books'}.spec.js`);
    c.results.forEach((r, i) => {
      assert.strictEqual(r.retry, i);
      assert.ok(!Number.isNaN(Date.parse(r.startTime)) && typeof r.duration === 'number');
      assert.strictEqual(r.errors.length > 0, r.status === 'failed'); assert.strictEqual(!!r.error, r.status === 'failed');
      r.steps.forEach((s, n) => assert.strictEqual(s.title, `${n + 1}. ${plans[code].steps[n].name}`, 'steps carry the declared names, in order'));
    });
    checkFiles(c);
    statuses[code] = c.results.map(r => r.status).join(',');
  }
  assert.deepStrictEqual(statuses, { 'WEB-DEM-01': 'passed', 'WEB-DEM-02': 'passed', 'WEB-DEM-03': 'failed,failed', 'WEB-DEM-04': 'passed', 'WEB-DEM-05': 'failed,failed',
    'WEB-DEM-06': 'skipped', 'WEB-DEM-07-ENV': 'failed,passed', 'WEB-DEM-08': 'passed' });

  const stamp = stampOf(ROOT);
  assert.ok(/^[0-9a-f]{64}$/.test(stamp), `--stamp prints a sha256: ${stamp}`);
  const levels = { OK: 0, MINOR: 0, BAD: 0, notCompared: 0 };
  const count = (u) => { for (const s of u.states) levels[s.level || 'notCompared']++; };

  // 01: three screens that match (a page without data, the list, a dialog)
  {
    const c = cases['WEB-DEM-01'], r = c.results[0];
    assert.strictEqual(r.steps.length, 3);
    const u = checkUi(c, r, { sent: [['Ban đầu · HP-1 Trợ giúp', 1, 'OK'], ['Sau thao tác · BK-1 Danh sách', 2, 'OK'], ['Màn cuối · BK-2 Thêm sách', 3, 'OK']],
      states: [['INITIAL', 1, 'HP-1', 'page', 'OK', true], ['AFTER_ACTION', 2, 'BK-1', 'page', 'OK', true], ['FINAL', 3, 'BK-2', 'dialog', 'OK', true]] }, stamp);
    count(u);
    // what the profile took out is kept, one line each, with its reason
    assert.strictEqual(u.skipped.length, 2);
    assert.ok(/^\[AFTER_ACTION\] BK-1 BAD Chữ "Cập nhật lúc 10:32" · .+ — Gói v0\.1: /.test(u.skipped[0]), u.skipped[0]);
    assert.ok(/^\[FINAL\] BK-2 MINOR Ô "Mã sách" · vòng focus: .+ — Cách đo: /.test(u.skipped[1]), u.skipped[1]);
    assert.ok(u.info.parts['ui-1'].includes('thanh đầu trang') && u.info.parts['ui-3'].includes('hộp thoại'), 'the profile names the part of a screen, the runtime the part of a dialog');
    none(r, 'vung-lech'); none(r, 'video');
  }
  // 02: a small seeded difference; two screens in step 2, the later one is sent
  {
    const c = cases['WEB-DEM-02'], r = c.results[0];
    const u = checkUi(c, r, { sent: [['Ban đầu · BK-1 Danh sách', 1, 'OK'], ['Màn cuối · BK-2 Thêm sách', 2, 'MINOR'], ['Màn cuối · BK-1 Danh sách', 3, 'OK']],
      states: [['INITIAL', 1, 'BK-1', 'page', 'OK', true], ['AFTER_ACTION', 2, 'BK-2', 'dialog', 'MINOR', false], ['FINAL', 2, 'BK-2', 'dialog', 'MINOR', true], ['FINAL', 3, 'BK-1', 'page', 'OK', true]] }, stamp);
    count(u);
    assert.deepStrictEqual(u.checks[1].diffs.map(d => `${d.type}:${d.level}`).sort(), ['RADIUS:MINOR', 'SPACING:MINOR']);
    none(r, 'vung-lech');
  }
  // 03: a heavy seeded difference: the step fails softly, the next steps run, the framed image is attached
  for (const r of cases['WEB-DEM-03'].results) {
    const c = cases['WEB-DEM-03'];
    const u = checkUi(c, r, { sent: [['Ban đầu · BK-1 Danh sách', 1, 'BAD'], ['Sau thao tác · BK-2 Thêm sách', 2, 'OK'], ['Màn cuối · BK-2 Thêm sách', 3, 'OK']],
      states: [['INITIAL', 1, 'BK-1', 'page', 'BAD', true], ['AFTER_ACTION', 2, 'BK-2', 'dialog', 'OK', true], ['FINAL', 3, 'BK-2', 'dialog', 'OK', true]] }, stamp);
    if (r.retry === 1) count(u);
    assert.deepStrictEqual(u.checks[0].diffs.map(d => `${d.type}:${d.level}`).sort(), ['BACKGROUND_COLOR:BAD', 'BACKGROUND_COLOR:BAD', 'FONT:BAD']);
    assert.strictEqual(r.errors.length, 1);
    const first = r.error.message.split('\n')[0];
    // Playwright puts the name of the error in front; what the check wrote starts right after it
    assert.ok(first.startsWith('Error: Giao diện lệch nặng so với design v0.1 (BK-1, màn đầu): Nút "Thêm sách": design màu nền: '), first);
    assert.ok(first.includes(', thực tế ') && first.includes(' | '), first);
    assert.strictEqual(r.steps.length, 3, 'a soft failure does not stop the case');
    assert.deepStrictEqual(r.steps.map(s => !!s.error), [true, false, false]);
    assert.strictEqual(r.steps[0].error.message, r.error.message);
    const framed = bytes(one(r, 'vung-lech'));
    assert.ok(framed.subarray(0, 8).equals(PNG) && !framed.equals(bytes(one(r, 'ui-1'))), 'the failure image is the screenshot with frames');
    assert.strictEqual(one(r, 'video').contentType, 'video/webm');
  }
  // 04: the web shows another object, and a page of the package that takes no data: not compared, the case passes
  {
    const c = cases['WEB-DEM-04'], r = c.results[0];
    const u = checkUi(c, r, { sent: [['Ban đầu · BK-1 Danh sách', 1, 'OK'], ['Màn cuối · BK-3 Chi tiết sách', 3, 'OK']],
      states: [['INITIAL', 1, 'BK-1', 'page', 'OK', true], ['AFTER_ACTION', 2, 'BK-3', 'dialog', null, false], ['FINAL', 3, 'BK-3', 'dialog', 'OK', true], ['FINAL', 3, 'RP-1', null, null, false]] }, stamp);
    count(u);
    assert.strictEqual(u.states[1].why, 'không so được: design sẽ vẽ sách "Dế Mèn phiêu lưu ký", màn web không có (web đang ở sách khác)');
    assert.strictEqual(u.states[3].why, 'gói design chưa nhận dữ liệu từ ngoài cho màn này');
    assert.ok(u.skipped.includes(`[AFTER_ACTION] BK-3 — ${u.states[1].why}`) && u.skipped.includes(`[FINAL] RP-1 — ${u.states[3].why}`));
    assert.strictEqual(r.steps.length, 3); assert.ok(r.steps.every(s => !s.error));
  }
  // 05: a functional failure: the steps after it never ran; the test's own context was filmed
  for (const r of cases['WEB-DEM-05'].results) {
    const c = cases['WEB-DEM-05'];
    assert.strictEqual(r.steps.length, 2, 'the step after the failure is not there');
    assert.deepStrictEqual(r.steps.map(s => !!s.error), [false, true]);
    assert.ok(!r.error.message.includes('Giao diện lệch nặng') && /toBeVisible/.test(r.error.message));
    const u = checkUi(c, r, { sent: [['Ban đầu · BK-2 Thêm sách', 1, 'OK']], states: [['INITIAL', 1, 'BK-2', 'dialog', 'OK', true]] }, stamp);
    if (r.retry === 1) count(u);
    none(r, 'vung-lech');
    assert.ok(bytes(one(r, 'video')).length > 1000, 'the film of the context the test opened itself');
  }
  // 08: a menu: only the menu is compared; then the list in the order the web shows it after sorting
  {
    const c = cases['WEB-DEM-08'], r = c.results[0];
    const u = checkUi(c, r, { sent: [['Ban đầu · BK-1 Danh sách', 1, 'OK'], ['Sau thao tác · BK-4 Menu sắp xếp', 2, 'OK'], ['Màn cuối · BK-1 Danh sách', 3, 'OK']],
      states: [['INITIAL', 1, 'BK-1', 'page', 'OK', true], ['AFTER_ACTION', 2, 'BK-4', 'menu', 'OK', true], ['FINAL', 3, 'BK-1', 'page', 'OK', true]] }, stamp);
    count(u);
    assert.ok(u.info.parts['ui-2'].includes('menu'));
  }
  // 06: skipped with a reason; 07: failed once, passed on the retry
  assert.deepStrictEqual(cases['WEB-DEM-06'].annotations, [{ type: 'skip', description: 'Chờ ticket KS-12: sản phẩm chưa có nút "Xuất báo cáo"' }]);
  assert.deepStrictEqual([cases['WEB-DEM-06'].results[0].steps, cases['WEB-DEM-06'].results[0].attachments], [[], []]);
  assert.ok(cases['WEB-DEM-07-ENV'].results[0].steps[0].error && !cases['WEB-DEM-07-ENV'].results[1].steps[0].error);
  assert.deepStrictEqual(cases['WEB-DEM-07-ENV'].results[1].attachments, [], 'a test that passed keeps no film');

  // results were on disk as each attempt ended (noted by lib/seen-reporter.js at that moment)
  const seen = fs.readFileSync(path.join(DIR, 'seen.jsonl'), 'utf8').trim().split('\n').map(l => JSON.parse(l));
  assert.strictEqual(seen.length, 11, 'eleven attempts in all');
  const final = new Set();
  for (const s of seen) {
    assert.ok(!s.problem, s.problem);
    assert.strictEqual(s.file, `${s.id}-${PROJECT}.json`, `the case file of "${s.title}" was there when its attempt ${s.retry} ended`);
    assert.deepStrictEqual([s.attempts, s.last], [s.retry + 1, s.status], 'and already held that attempt');
    if (!(s.status === 'failed' && s.retry === 0)) final.add(s.id);
    assert.deepStrictEqual([s.run.status, s.run.finished, s.run.tests, s.run.done], ['running', null, 8, final.size], 'run.json follows the run');
  }
  // nothing of a recording is kept twice, and nothing of a test that passed
  const left = [];
  const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) { if (e.name === 'videos') left.push(p); walk(p); } else if (e.name.endsWith('.webm') && !p.includes(`${path.sep}attachments${path.sep}`)) left.push(p); } };
  walk(path.join(DIR, 'artifacts'));
  assert.deepStrictEqual(left, [], 'no recording left outside the attachments of failed tests');

  assert.deepStrictEqual(levels, { OK: 13, MINOR: 2, BAD: 1, notCompared: 2 }, 'the levels of every check of the suite');
  const stamps = checkStamp(stamp);

  // the label map of the profile, checked on the design page alone: every key comes back but the one no screen shows
  const trip = JSON.parse(execFileSync(process.execPath, [path.join('autotest', 'uicheck', 'roundtrip.js'), 'BK-1', path.join('lib', 'ui-profile.map.json')],
    { cwd: path.join(ROOT, 'web'), encoding: 'utf8' }));
  assert.deepStrictEqual([trip.lists.books.rows, trip.lists.books.lost.map(l => `${l.key} ${l.rows}/${l.of}`), trip.lists.books.unmapped, trip.notShown, trip.errors],
    [{ design: 3, read: 3 }, ['id 3/3'], [], [], []], 'roundtrip.js on state BK-1');
  console.log(`main      8 tests, 11 attempts: 4 passed, 2 failed twice, 1 passed on its retry, 1 skipped; case files on disk at the end of each attempt`);
  console.log(`          UI: ${levels.OK} OK, ${levels.MINOR} MINOR, ${levels.BAD} BAD, ${levels.notCompared} not compared; ui-info.stamp = --stamp (${stamp.slice(0, 12)}…)`);
  console.log(`          stamp: changes with ${stamps.changes.join(', ')}; stays with ${stamps.stays.join(', ')}`);
  console.log('          label map on the design page alone: every key of a book comes back but its id, which no screen shows');
}

/** The stamp changes when the comparer, the profile, the thresholds or the package changes, and only then. */
function checkStamp(stamp) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'autotest-stamp-'));
  try {
    const copy = (name) => {
      const to = path.join(tmp, name);
      // the test repository only: not its results, not the second repository of the fixture
      fs.cpSync(ROOT, to, { recursive: true, filter: (src) => !/^(edge|web[\\/](results|test-results|node_modules))([\\/]|$)/.test(path.relative(ROOT, src)) });
      return to;
    };
    const settings = (root, edit) => { const f = path.join(root, 'autotest.json'); const c = readJson(f); edit(c); fs.writeFileSync(f, JSON.stringify(c, null, 1) + '\n'); };
    const append = (root, rel, what) => fs.appendFileSync(path.join(root, ...rel.split('/')), what);
    const cases = {
      'another folder': [false, () => {}],
      'the threshold preset': [true, (r) => settings(r, c => { c.conventions.ui_threshold.value = 'strict'; })],
      'the size the share applies from': [true, (r) => settings(r, c => { c.conventions.ui_percent_from.value = 8; })],
      'the share switched off': [true, (r) => settings(r, c => { c.conventions.ui_percent_from.value = 'off'; })],
      'no threshold chosen': [true, (r) => settings(r, c => { delete c.conventions.ui_threshold; })],
      'the profile': [true, (r) => append(r, 'web/lib/ui-profile.js', '\n// changed\n')],
      'the label map of the profile': [true, (r) => append(r, 'web/lib/ui-profile.map.json', '\n')],
      'the design package': [true, (r) => { const f = path.join(r, 'design-src', 'v0.1', 'autotest-states.json'); const i = readJson(f); i.sha256 = '0'.repeat(64); fs.writeFileSync(f, JSON.stringify(i)); }],
      'a file of the comparer': [true, (r) => append(r, 'web/autotest/uicheck/engine.js', '\n// changed\n')],
      'what a BAD screen does to its case': [false, (r) => settings(r, c => { c.conventions.ui_bad.value = 'column'; })],
      'other settings': [false, (r) => settings(r, c => { c.project.name = 'Tên khác'; c.hub = { url: 'https://example.test' }; })],
      'a helper of the project': [false, (r) => append(r, 'web/lib/product.js', '\n// changed\n')],
      'the rest of the runtime': [false, (r) => { append(r, 'web/autotest/plan.js', '\n// changed\n'); fs.writeFileSync(path.join(r, 'web', 'autotest', 'VERSION'), 'abc\n'); }],
      'a file a file browser leaves': [false, (r) => fs.writeFileSync(path.join(r, 'web', 'autotest', 'uicheck', '.DS_Store'), 'x')],
    };
    const out = { changes: [], stays: [] }, seen = new Set([stamp]);
    let n = 0;
    for (const [what, [changes, edit]] of Object.entries(cases)) {
      const root = copy(`v${n++}`);
      edit(root);
      const s = stampOf(root);
      if (changes) { assert.ok(!seen.has(s), `the stamp must change with ${what}`); seen.add(s); out.changes.push(what); }
      else { assert.strictEqual(s, stamp, `the stamp must not change with ${what}`); out.stays.push(what); }
    }
    return out;
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
}

// ---------------------------------------------------------------- WEB-DEM-03 alone, under another choice
function alone() {
  const run = readJson(path.join(DIR, 'run.json'));
  assert.deepStrictEqual([run.status, run.tests, run.done], ['passed', 1, 1], 'the case passes');
  const cases = load();
  const c = cases['WEB-DEM-03'];
  assert.deepStrictEqual([Object.keys(cases), c.results.map(r => r.status)], [['WEB-DEM-03'], ['passed']]);
  const r = c.results[0];
  checkFiles(c);
  assert.deepStrictEqual([r.errors, r.steps.length, r.steps.some(s => s.error)], [[], 3, false]);
  none(r, 'vung-lech'); none(r, 'video');
  const stamp = stampOf(ROOT);
  if (mode === 'column') {
    // the screen is still judged BAD and sent; only the case is not failed by it
    checkUi(c, r, { sent: [['Ban đầu · BK-1 Danh sách', 1, 'BAD'], ['Sau thao tác · BK-2 Thêm sách', 2, 'OK'], ['Màn cuối · BK-2 Thêm sách', 3, 'OK']],
      states: [['INITIAL', 1, 'BK-1', 'page', 'BAD', true], ['AFTER_ACTION', 2, 'BK-2', 'dialog', 'OK', true], ['FINAL', 3, 'BK-2', 'dialog', 'OK', true]] }, stamp);
    console.log('column    WEB-DEM-03 passes with its BAD screen sent, no failed step, no framed failure image');
  } else {
    // nothing is judged: three states recorded as not compared, nothing sent
    const u = checkUi(c, r, { sent: [], states: [['INITIAL', 1, 'BK-1', null, null, false], ['AFTER_ACTION', 2, 'BK-2', null, null, false], ['FINAL', 3, 'BK-2', null, null, false]] }, stamp);
    assert.ok(u.states.every(s => s.why === NOT_CHOSEN));
    assert.deepStrictEqual(u.skipped, ['[INITIAL] BK-1', '[AFTER_ACTION] BK-2', '[FINAL] BK-2'].map(s => `${s} — ${NOT_CHOSEN}`));
    none(r, 'ui_checks');
    console.log(`unchosen  WEB-DEM-03 passes, its three screens recorded as "${NOT_CHOSEN}", nothing sent`);
  }
}

try {
  if (mode === 'main') main();
  else if (mode === 'column' || mode === 'unchosen') alone();
  else { console.error('usage: node check.js main|column|unchosen <fixture root> <results dir>'); process.exit(2); }
} catch (e) {
  console.error(`NOT AS EXPECTED (${mode}): ${e.message}`);
  process.exit(1);
}
