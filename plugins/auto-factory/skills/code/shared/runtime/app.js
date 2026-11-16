// The app under test on a virtual phone: what a browser page is on the web. Used by the tests of a mobile project
// (through test.js: the `app` fixture) and by the factory's UI checks (factory-ui.js), so both drive a phone one way.
//
// tx / fx start the phones, install the build and start Appium before the tests run, and say so in AUTOTEST_MOBILE
// (or FACTORY_UI_MOBILE): a JSON text or the path of a JSON file
//   { appium: "http://127.0.0.1:4723", devices: [{ key, platform, name, os, screen, size: [w, h], app: { id }, app_file,
//     capabilities }] }
// A Playwright project of a mobile project is one device: its `use.device` is the device's key.
//
//   const app = await openApp(device)          a session on the device, the app started with its data cleared
//   await tap(app, 'Next')                     the element a user sees with this text or label
//   await type(app, 'Email', 'a@b.test')       the field with this label or hint
//   await see(app, 'Welcome!')                 waits until the text is on the screen (throws when it is not)
//   await notSee(app, 'Skip')
//   await shot(app)                            a PNG of the screen (Buffer)
//   await restart(app)                         closes and opens the app again, its data kept
//   await background(app, 3)                   the app in the background for 3 s
//   await openLink(app, 'c2p://home')          a deep link
//   await crashes(app)                         what the app reported as a crash or an uncaught error since the last call
//   await closeApp(app)
//
// Nothing here looks inside the app: it sees what a user sees, through the accessibility tree of the phone.
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');

function load(name) {
  const dirs = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
  for (const d of dirs) { try { return require(path.join(d, name)); } catch (e) { /* next */ } }
  return require(name);
}

let SETUP;
/** What tx / fx said about the phones of this run. */
function mobileSetup() {
  if (SETUP) return SETUP;
  const raw = process.env.AUTOTEST_MOBILE || process.env.FACTORY_UI_MOBILE || '';
  if (!raw) throw new Error('app: không có máy nào được bật cho lượt này (AUTOTEST_MOBILE trống)');
  SETUP = JSON.parse(raw.trim().startsWith('{') ? raw : fs.readFileSync(raw, 'utf8'));
  return SETUP;
}

/** The device of a key ("android-360x640"), or the first one. */
function device(key) {
  const S = mobileSetup();
  const d = key ? S.devices.find(x => x.key === key) : S.devices[0];
  if (!d) throw new Error(`app: máy ${key} không được bật cho lượt này (có: ${S.devices.map(x => x.key).join(', ')})`);
  return d;
}

const sh = (cmd, args) => new Promise((res) => execFile(cmd, args, { timeout: 120000 }, (e, out) => res(e ? null : String(out || ''))));
const appArg = (d) => (d.platform === 'ios' ? { bundleId: d.app.id } : { appId: d.app.id });

/** A session on the device. fresh: the app's data cleared first, as on a phone it was just installed on. */
async function openApp(dev, { fresh = true } = {}) {
  const d = typeof dev === 'string' ? device(dev) : dev || device();
  const { remote } = load('webdriverio');
  const u = new URL(mobileSetup().appium);
  const t0 = Date.now();
  let app;
  try {
    app = await remote({ protocol: u.protocol.replace(':', ''), hostname: u.hostname, port: Number(u.port), path: '/',
      capabilities: d.capabilities, logLevel: 'error', connectionRetryTimeout: 360000, connectionRetryCount: 1 });
  } catch (e) {
    // the app may have crashed while it started: say why, from the simulator's own crash report
    const why = iosCrashes(d, t0 - 5000)[0];
    if (why) throw new Error(`app crashed while it started on ${d.key}: ${why.text}`);
    throw e;
  }
  app.__since = t0;
  app.device = d;
  app.__crashSeen = 0;
  if (fresh) await reset(app); else await app.execute('mobile: activateApp', appArg(d));
  return app;
}

/** The app as just installed: stopped, its data (and on iOS the simulator's keychain) cleared, its permissions given. */
async function reset(app) {
  const d = app.device;
  await app.execute('mobile: terminateApp', appArg(d)).catch(() => {});
  if (d.platform === 'android') {
    await app.execute('mobile: clearApp', { appId: d.app.id });
    await app.execute('mobile: changePermissions', { permissions: 'all', appPackage: d.app.id, action: 'grant' }).catch(() => {});
    await app.execute('mobile: shell', { command: 'logcat', args: ['-c'] }).catch(() => {});
  } else {
    let cleared = false;
    try { await app.execute('mobile: clearApp', appArg(d)); cleared = true; } catch (e) { /* older driver */ }
    if (!cleared && d.app_file) { await app.removeApp(d.app.id).catch(() => {}); await app.installApp(d.app_file); }
    // what an app keeps in the keychain outlives its data: on the factory's own simulator it is emptied too
    const udid = d.capabilities['appium:udid'];
    if (udid && process.platform === 'darwin') await sh('xcrun', ['simctl', 'keychain', udid, 'reset']);
  }
  await app.execute('mobile: activateApp', appArg(d));
}

async function restart(app) {
  await app.execute('mobile: terminateApp', appArg(app.device)).catch(() => {});
  await app.execute('mobile: activateApp', appArg(app.device));
}

async function background(app, seconds = 3) {
  app.__backgrounded = true;   // not a crash: crashes() does not count it
  try { await app.execute('mobile: backgroundApp', { seconds }); } finally { app.__backgrounded = false; }
}

async function openLink(app, url) {
  const d = app.device;
  if (d.platform === 'android') await app.execute('mobile: deepLink', { url, package: d.app.id });
  else await app.execute('mobile: deepLink', { url, bundleId: d.app.id });
}

async function closeApp(app) {
  if (!app) return;
  await app.execute('mobile: terminateApp', appArg(app.device)).catch(() => {});
  await app.deleteSession().catch(() => {});
}

// ---------------------------------------------------------------- finding what a user sees
const xq = (s) => (String(s).includes('"') ? `concat("${String(s).split('"').join('", \'"\', "')}")` : `"${s}"`);
const pq = (s) => `'${String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;

/** The selector of an element with this text, label or hint (exact; `{ partial: true }` for a part of it). */
function selector(app, text, { partial = false } = {}) {
  if (app.device.platform === 'ios') {
    const op = partial ? 'CONTAINS' : '==';
    return `-ios predicate string:label ${op} ${pq(text)} OR name ${op} ${pq(text)} OR value ${op} ${pq(text)} OR placeholderValue ${op} ${pq(text)}`;
  }
  const f = partial ? (a) => `contains(@${a}, ${xq(text)})` : (a) => `@${a}=${xq(text)}`;
  return `//*[${f('text')} or ${f('content-desc')} or ${f('hint')}]`;
}

async function find(app, text, opts = {}) {
  const timeout = opts.timeout === undefined ? 15000 : opts.timeout;
  const el = await app.$(selector(app, text, opts));
  await el.waitForDisplayed({ timeout, timeoutMsg: `không thấy "${text}" trên màn sau ${Math.round(timeout / 1000)} giây` });
  return el;
}

async function tap(app, text, opts = {}) {
  const el = await find(app, text, opts);
  await el.click();
  return el;
}

/** Type into the field with this label or hint; the earlier value is cleared. */
async function type(app, label, value, opts = {}) {
  const el = await find(app, label, opts);
  await el.click();
  await el.clearValue().catch(() => {});
  await el.setValue(String(value));
  if (opts.hideKeyboard !== false) await app.hideKeyboard().catch(() => {});
  return el;
}

async function see(app, text, opts = {}) { return find(app, text, opts); }

async function notSee(app, text, opts = {}) {
  const timeout = opts.timeout === undefined ? 5000 : opts.timeout;
  const el = await app.$(selector(app, text, opts));
  await el.waitForDisplayed({ timeout, reverse: true, timeoutMsg: `"${text}" vẫn còn trên màn sau ${Math.round(timeout / 1000)} giây` });
}

/** Swipe the screen: direction up | down | left | right, over a share of it (0.2 to 0.9). */
async function swipe(app, direction, share = 0.6) {
  const r = await app.getWindowRect();
  const cx = r.width / 2, cy = r.height / 2, dx = (r.width * share) / 2, dy = (r.height * share) / 2;
  const [x0, y0, x1, y1] = { left: [cx + dx, cy, cx - dx, cy], right: [cx - dx, cy, cx + dx, cy], up: [cx, cy + dy, cx, cy - dy], down: [cx, cy - dy, cx, cy + dy] }[direction];
  await app.action('pointer', { parameters: { pointerType: 'touch' } })
    .move({ x: Math.round(x0), y: Math.round(y0) }).down().pause(80)
    .move({ duration: 350, x: Math.round(x1), y: Math.round(y1) }).up().perform();
}

async function back(app) {
  if (app.device.platform === 'android') await app.back();
  else await swipeFromEdge(app);
}

async function swipeFromEdge(app) {
  const r = await app.getWindowRect();
  await app.action('pointer', { parameters: { pointerType: 'touch' } })
    .move({ x: 2, y: Math.round(r.height / 2) }).down().pause(50)
    .move({ duration: 300, x: Math.round(r.width * 0.7), y: Math.round(r.height / 2) }).up().perform();
}

async function shot(app) { return Buffer.from(await app.takeScreenshot(), 'base64'); }

/** The system dialog asking for a permission, answered (allow: true / false) when it is shown. */
async function answerPermission(app, allow = true, timeout = 3000) {
  const words = allow ? ['Allow', 'Allow While Using App', 'While using the app', 'Cho phép', 'OK'] : ["Don't Allow", "Don’t Allow", "Don't allow", 'Không cho phép'];
  for (const w of words) {
    const el = await app.$(selector(app, w));
    if (await el.isDisplayed().catch(() => false)) { await el.click(); return w; }
  }
  await app.pause(Math.min(timeout, 500));
  return null;
}

// ---------------------------------------------------------------- what went wrong in the app
/**
 * What the app reported as a crash or an uncaught error since the last call: [{ text }]. Android: the system's crash
 * log and Flutter's error lines of the app's process; iOS: the app is no longer running when it should be.
 */
async function crashes(app) {
  const d = app.device, out = [];
  if (d.platform === 'android') {
    const logs = (await app.getLogs('logcat').catch(() => [])).map(l => String(l.message || ''));
    logs.forEach((m, i) => {
      // only this app's: a FATAL EXCEPTION names its process on the next lines; Flutter's errors are the app's own
      const fatal = /FATAL EXCEPTION/.test(m) && logs.slice(i, i + 4).some(x => x.includes('Process: ' + d.app.id));
      const flutter = /E\/flutter|E flutter|Unhandled Exception/.test(m);
      const runtime = /AndroidRuntime.*(Exception|Error)/.test(m) && m.includes(d.app.id);
      if (fatal || flutter || runtime) out.push({ text: m.replace(/^\S+\s+\S+\s+\d+\s+\d+\s+/, '').slice(0, 400) });
    });
  }
  if (d.platform === 'ios') {
    for (const c of iosCrashes(d, app.__since || 0)) out.push(c);
    app.__since = Date.now();
  }
  const state = await app.execute('mobile: queryAppState', appArg(d)).catch(() => null);
  if (state !== null && state !== 4 && !app.__backgrounded) out.push({ text: `the app is not running in the foreground any more (state ${state}): it may have crashed` });
  return out;
}

/** The crash reports macOS wrote for the app on this simulator since `since` (ms): what it died of and where, in one
 * line each. A simulator's app crashes on this machine, so its reports are in ~/Library/Logs/DiagnosticReports. */
function iosCrashes(d, since, dir) {
  if (d.platform !== 'ios' || (!dir && process.platform !== 'darwin')) return [];
  const udid = (d.capabilities || {})['appium:udid'] || '';
  dir = dir || path.join(require('os').homedir(), 'Library', 'Logs', 'DiagnosticReports');
  const out = [];
  let names = [];
  try { names = fs.readdirSync(dir).filter(n => n.endsWith('.ips')); } catch { return []; }
  for (const n of names) {
    const f = path.join(dir, n);
    try {
      if (fs.statSync(f).mtimeMs < since) continue;
      const txt = fs.readFileSync(f, 'utf8');
      const nl = txt.indexOf('\n');
      const head = JSON.parse(txt.slice(0, nl));
      if (head.bundleID !== d.app.id) continue;
      const body = JSON.parse(txt.slice(nl + 1));
      if (udid && !String(body.procPath || '').includes(udid)) continue;
      const ex = body.exception || {};
      const th = (body.threads || [])[body.faultingThread || 0] || {};
      const frame = (th.frames || []).find(x => x.symbol) || {};
      const img = (body.usedImages || [])[frame.imageIndex] || {};
      const reason = ((body.asi && Object.values(body.asi).flat().join(' ')) || '').slice(0, 200);
      out.push({ text: `${ex.type || 'crash'} (${ex.signal || ''}) in ${img.name || '?'} ${frame.symbol || ''}${reason ? ': ' + reason : ''}`.trim(),
        file: f });
    } catch { /* not a report this reads */ }
  }
  return out;
}

// ---------------------------------------------------------------- video of a test
/** Start filming the screen. Returns a stop() that gives the MP4 (Buffer), or null when the device cannot film. */
async function film(app, dir) {
  const d = app.device;
  if (d.platform === 'ios' && process.platform === 'darwin') {
    // a simulator films itself (simctl); Appium would need ffmpeg for it
    const udid = d.capabilities['appium:udid'];
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `video-${Date.now()}.mp4`);
    const { spawn } = require('child_process');
    const p = spawn('xcrun', ['simctl', 'io', udid, 'recordVideo', '--codec=h264', '--force', file], { stdio: 'ignore' });
    await new Promise(r => setTimeout(r, 800));
    return async () => {
      if (p.exitCode === null) { p.kill('SIGINT'); await new Promise(r => { p.once('exit', r); setTimeout(r, 15000); }); }
      try { const b = fs.readFileSync(file); fs.rmSync(file, { force: true }); return b.length ? b : null; } catch { return null; }
    };
  }
  if (d.platform === 'android') {
    // one screenrecord of Android lasts at most 180 s (longer needs ffmpeg to join the pieces); the film has the
    // screen's own shape, 1280 high (the encoder's default frame is 720x1280 with bars around a taller screen)
    const [w, h] = d.size || [];
    const opts = { timeLimit: 180, bitRate: 2000000 };
    if (w > 0 && h > 0) opts.videoSize = `${Math.round((1280 * w) / h / 2) * 2}x1280`;
    await app.startRecordingScreen(opts).catch(() => app.startRecordingScreen({ timeLimit: 180, bitRate: 2000000 }).catch(() => null));
    return async () => {
      const b64 = await app.stopRecordingScreen().catch(() => null);
      return b64 ? Buffer.from(b64, 'base64') : null;
    };
  }
  return async () => null;
}

module.exports = { mobileSetup, device, openApp, iosCrashes, reset, restart, background, openLink, closeApp, selector, find, tap, type,
  see, notSee, swipe, back, shot, answerPermission, crashes, film };
