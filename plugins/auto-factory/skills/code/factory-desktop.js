// factory-desktop.js: a Tauri 2 app's front end in Playwright's Chromium, its IPC answered here (factory-ui.js uses it).
//
// The page gets what Tauri's runtime puts in a window before the app's code runs: window.isTauri, and
// window.__TAURI_INTERNALS__ (invoke, transformCallback, runCallback, metadata, convertFileSrc), the event plugin's
// internals and the OS plugin's values, the way @tauri-apps/api 2 reads them. Its `invoke` hands every call to this
// process (one Playwright binding), where it is answered, in this order, by:
//
//   1. the check script's own handlers:  t.ipc.handle('get_settings', (args, ctx) => ({ theme: 'dark' }))
//   2. the project's mocks file (desktop.mocks in factory.json), a CommonJS module of the same handlers:
//        module.exports = {
//          get_settings: () => ({ theme: 'light' }),
//          save_settings: (args, { state }) => { state.saved = args.settings; return null; },
//          load_report: async (args, { channel }) => { channel(args.onProgress).send(50); return { rows: [] }; },
//          'plugin:dialog|open': () => 'C:\\Users\\factory\\Documents\\report.csv',
//        };
//      A handler is a function (args, ctx) or a plain value. Throwing rejects the call with that error, the way a
//      Rust command's Err does. ctx: { cmd, state (one object for a whole run of the script), emit(event, payload),
//      channel(channelArg) -> { send(message), end() }, t }.
//   3. built-in answers for Tauri's own plugins that only describe the window or the machine (window, app, os,
//      opener, notification, log), as a Windows machine would.
//
// A call nothing answers is rejected and listed in the report (ipc.missing): the check fails, since the real app's
// Rust side would have answered it. Every call is listed in ipc.calls; a script asserts on them with t.ipc.calls(cmd).
// Events: the page's listen/unlisten/emit stay in the page, as in Tauri's own mockIPC(shouldMockEvents); t.ipc.emit
// sends one from the "Rust side" to the page.

'use strict';
const fs = require('fs');

const MAX_CALLS = 500;      // kept in the report, per way of running; t.ipc.calls() sees every call of its run
const MAX_SEEN = 50000;

// The calls of one way of running (`desktop` or `app`): all of them for t.ipc.calls(), the first MAX_CALLS in the report.
function ipcLog(report, variant) {
  const seen = [];
  return {
    seen,
    add(call) {
      call.variant = variant;
      if (seen.length < MAX_SEEN) seen.push(call);
      const kept = report.ipc.kept = report.ipc.kept || {};
      if ((kept[variant] || 0) < MAX_CALLS) {
        kept[variant] = (kept[variant] || 0) + 1;
        report.ipc.calls.push(call);
      } else {
        report.ipc.calls_dropped = (report.ipc.calls_dropped || 0) + 1;
      }
    },
  };
}

function callsOf(seen, cmd) {
  return seen.filter((c) => !cmd || c.cmd === cmd).map((c) => {
    let args; try { args = JSON.parse(c.args); } catch (e) { args = c.args; }
    return { cmd: c.cmd, args, answered_by: c.answered_by, error: c.error, status: c.status };
  });
}
const MAX_ARGS = 2000;

// What runs in the page before its own scripts. Kept to what @tauri-apps/api 2.x reads.
function pageScript(setup) {
  const app = setup.app || {};
  const os = {
    platform: 'windows', os_type: 'windows', family: 'windows', arch: 'x86_64', version: '10.0.26100',
    exe_extension: 'exe', eol: '\r\n',
  };
  return `(() => {
  if (window.__FACTORY_TAURI__) return;
  const callbacks = new Map();
  const listeners = new Map();
  const channelIndex = new Map();
  const internals = window.__TAURI_INTERNALS__ = window.__TAURI_INTERNALS__ || {};
  Object.defineProperty(window, 'isTauri', { value: true, configurable: true });
  function register(cb, once) {
    const id = window.crypto.getRandomValues(new Uint32Array(1))[0];
    const fn = (data) => { if (once) unregister(id); return cb && cb(data); };
    callbacks.set(id, fn);
    try { Object.defineProperty(window, '_' + id, { value: fn, configurable: true }); } catch (e) { /* older readers */ }
    return id;
  }
  function unregister(id) { callbacks.delete(id); try { delete window['_' + id]; } catch (e) { /* gone */ } }
  function run(id, data) { const f = callbacks.get(id); if (f) f(data); }
  function deliver(event, payload) {
    for (const h of [...(listeners.get(event) || [])]) run(h, { event, id: h, payload });
  }
  internals.transformCallback = register;
  internals.unregisterCallback = unregister;
  internals.runCallback = run;
  internals.callbacks = callbacks;
  internals.metadata = { currentWindow: { label: 'main' }, currentWebview: { windowLabel: 'main', label: 'main' } };
  internals.plugins = Object.assign({}, internals.plugins, { path: { sep: '\\\\', delimiter: ';' } });
  internals.convertFileSrc = (p, protocol) => 'http://' + (protocol || 'asset') + '.localhost/' + encodeURIComponent(p);
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = Object.assign(window.__TAURI_EVENT_PLUGIN_INTERNALS__ || {}, {
    unregisterListener: (event, id) => unregister(id),
  });
  window.__TAURI_OS_PLUGIN_INTERNALS__ = ${JSON.stringify(os)};
  internals.invoke = async (cmd, args, options) => {
    args = args === undefined ? {} : args;
    if (cmd === 'plugin:event|listen') {
      if (!listeners.has(args.event)) listeners.set(args.event, []);
      listeners.get(args.event).push(args.handler);
      return args.handler;
    }
    if (cmd === 'plugin:event|unlisten') {
      const l = listeners.get(args.event) || [];
      const i = l.indexOf(args.eventId);
      if (i >= 0) l.splice(i, 1);
      return null;
    }
    let plain;
    try {
      // what crosses to Rust is JSON: a Channel becomes "__CHANNEL__:<id>", as Tauri sends it
      plain = args instanceof ArrayBuffer || ArrayBuffer.isView(args) ? { bytes: Array.from(new Uint8Array(args.buffer || args)) }
        : JSON.parse(JSON.stringify(args));
    } catch (e) { plain = { unreadable: String(e && e.message || e) }; }
    if (cmd === 'plugin:event|emit' || cmd === 'plugin:event|emit_to') deliver(args.event, args.payload);
    const r = await window.__factoryTauriInvoke({ cmd, args: plain, url: location.href });
    if (!r || !r.ok) throw (r ? r.error : 'no answer');
    return r.value;
  };
  window.__FACTORY_TAURI__ = {
    emit: deliver,
    channel(id, message, end) {
      const i = channelIndex.get(id) || 0;
      channelIndex.set(id, i + 1);
      run(id, end ? { index: i, end: true } : { index: i, message });
    },
  };
})();`;
}

// Tauri's own plugins that answer from the window or the machine, not from the app: a Windows machine's answers.
function builtin(cmd, args, info) {
  const [plugin, name] = cmd.startsWith('plugin:') ? cmd.slice(7).split('|') : [null, cmd];
  const vp = info.viewport() || { width: 800, height: 600 };
  if (plugin === 'window' || plugin === 'webview') {
    if (/^(inner|outer)_size$/.test(name)) return { found: true, value: { width: vp.width, height: vp.height } };
    if (/^(inner|outer)_position$/.test(name)) return { found: true, value: { x: 0, y: 0 } };
    if (name === 'scale_factor') return { found: true, value: 1 };
    if (name === 'title') return { found: true, value: info.app.title };
    if (name === 'theme') return { found: true, value: 'light' };
    if (/^is_(maximized|minimized|fullscreen|always_on_top)$/.test(name)) return { found: true, value: false };
    if (/^is_/.test(name)) return { found: true, value: true };
    return { found: true, value: null };   // a window that moves, resizes or closes: nothing to show in a browser
  }
  if (plugin === 'app') {
    const v = { version: info.app.version, name: info.app.name, tauri_version: '2', identifier: info.app.identifier };
    if (name in v) return { found: true, value: v[name] };
    return { found: true, value: null };
  }
  if (plugin === 'os') {
    if (name === 'locale') return { found: true, value: 'en-US' };
    if (name === 'hostname') return { found: true, value: 'factory-check' };
  }
  if (plugin === 'opener' || (plugin === 'shell' && name === 'open')) return { found: true, value: null };  // logged: t.ipc.calls
  if (plugin === 'notification') {
    if (name === 'is_permission_granted') return { found: true, value: true };
    if (name === 'request_permission') return { found: true, value: 'granted' };
    return { found: true, value: null };
  }
  if (plugin === 'log' || (plugin === 'event' && /^emit(_to)?$/.test(name))) return { found: true, value: null };
  return { found: false };
}

function short(v) {
  let s;
  try { s = JSON.stringify(v); } catch (e) { s = String(v); }
  return s && s.length > MAX_ARGS ? s.slice(0, MAX_ARGS) + '…' : s;
}

function loadMocks(file) {
  if (!file) return { handlers: {} };
  try {
    delete require.cache[require.resolve(file)];
    const m = require(file);
    if (!m || typeof m !== 'object') return { handlers: {}, error: 'the mocks file exports no object of commands' };
    return { handlers: m };
  } catch (e) {
    return { handlers: {}, error: String(e && e.message || e).split('\n')[0].slice(0, 300) };
  }
}

// Sets up one browser context as the app's window. Returns t.ipc for the script.
async function attach(context, report, setup, getPage, t) {
  const ipc = report.ipc;
  const mocks = loadMocks(setup.mocks);
  if (mocks.error) ipc.mocks_error = mocks.error;
  const own = {};
  const state = {};
  const info = { app: setup.app || {}, viewport: () => { const p = getPage(); return p && p.viewportSize(); } };
  const missing = new Set(ipc.missing);
  const log = ipcLog(report, 'desktop');

  const emit = async (event, payload) => {
    await getPage().evaluate(([e, p]) => window.__FACTORY_TAURI__.emit(e, p), [event, payload === undefined ? null : payload]);
  };
  const channel = (arg) => {
    const id = parseInt(String(arg || '').replace(/^__CHANNEL__:/, ''), 10);
    if (!id) throw new Error('not a channel: ' + short(arg));
    const send = (message, end) => getPage().evaluate(([i, m, e]) => window.__FACTORY_TAURI__.channel(i, m, e), [id, message === undefined ? null : message, !!end]);
    return { send: (m) => send(m, false), end: () => send(null, true) };
  };

  await context.exposeBinding('__factoryTauriInvoke', async (source, msg) => {
    const cmd = String(msg && msg.cmd || '');
    const args = (msg && msg.args) || {};
    const call = { cmd, args: short(args), url: msg && msg.url, answered_by: null };
    log.add(call);
    const ctx = { cmd, state, emit, channel, t };
    let h;
    if (Object.prototype.hasOwnProperty.call(own, cmd)) { h = own[cmd]; call.answered_by = 'script'; }
    else if (Object.prototype.hasOwnProperty.call(mocks.handlers, cmd)) { h = mocks.handlers[cmd]; call.answered_by = 'mocks'; }
    if (call.answered_by) {
      try {
        const value = typeof h === 'function' ? await h(args, ctx) : h;
        return { ok: true, value: value === undefined ? null : value };
      } catch (e) {
        call.error = short(e && e.message !== undefined ? e.message : e);
        return { ok: false, error: e && e.message !== undefined ? e.message : e };
      }
    }
    const b = builtin(cmd, args, info);
    if (b.found) { call.answered_by = 'built-in'; return { ok: true, value: b.value }; }
    call.answered_by = 'none';
    if (!missing.has(cmd)) { missing.add(cmd); ipc.missing.push(cmd); }
    return { ok: false, error: `no mock for the command ${cmd} (factory UI check: add it to the mocks file or t.ipc.handle)` };
  });
  await context.addInitScript({ content: pageScript(setup) });

  return {
    mocked: true,
    handle(cmd, handler) { own[String(cmd)] = handler; },
    calls: (cmd) => callsOf(log.seen, cmd),
    emit,
    channel,
    state,
  };
}

// ---------------------------------------------------------------- the real app on Windows (variant `app`)
//
// fx built the app and made this run a folder of Windows (setup.app_run): the .exe copied there, so it is stopped by its
// own path, and the WebView2 data folder of this run. The app is started with WebView2's DevTools port open and
// Playwright connects to it over CDP; its IPC is the real Rust side, seen as requests to http://ipc.localhost/<command>.
// While WSL's network is NAT, WSL cannot reach a port Windows opens on 127.0.0.1 (and Windows' firewall blocks a
// listener on its side of WSL's network): the connection goes through a tunnel, a port of this process each of whose
// connections is carried over the stdin and stdout of a small node.exe on Windows to the DevTools port.

const WRY_ARGS = '--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function appPage(context) {
  return context.pages().find((p) => /^https?:/.test(p.url()) && !/^https?:\/\/(ipc|asset)\.localhost/.test(p.url()));
}

// node.exe -e PIPE <port>: one connection, between its stdin/stdout and 127.0.0.1:<port> of Windows
const PIPE = "const s=require('net').connect(+process.argv[1],'127.0.0.1');process.stdin.pipe(s);s.pipe(process.stdout);" +
  "s.on('error',()=>process.exit(1));s.on('close',()=>process.exit(0));process.stdin.on('end',()=>s.end());";

function tunnel(nodeExe, port) {
  const net = require('net');
  const { spawn } = require('child_process');
  const kids = new Set();
  const srv = net.createServer((c) => {
    const p = spawn(nodeExe, ['-e', PIPE, String(port)], { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
    kids.add(p);
    c.pipe(p.stdin);
    p.stdout.pipe(c);
    c.on('error', () => p.kill());
    c.on('close', () => p.kill());
    p.stdin.on('error', () => c.destroy());
    p.on('exit', () => { kids.delete(p); c.destroy(); });
  });
  return new Promise((resolve, reject) => {
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => resolve({
      port: srv.address().port,
      close: () => { for (const k of kids) { try { k.kill(); } catch (e) { /* gone */ } } srv.close(); },
    }));
  });
}

async function launchApp(run, chromium) {
  const { spawn, spawnSync } = require('child_process');
  let child = null;
  const way = run.tunnel ? await tunnel(run.tunnel, run.port) : null;
  const cdp = `http://127.0.0.1:${way ? way.port : run.port}`;
  let exited = null;
  const stopAll = () => {
    // a Windows program started from WSL outlives its WSL process: stopped by its own .exe and WebView2 data folder
    try {
      spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', run.stop_ps],
                { stdio: 'ignore', timeout: 60000, windowsHide: true });
    } catch (e) { /* fx stops it again after the run */ }
    try { if (child) child.kill(); } catch (e) { /* gone */ }
  };
  const start = async () => {
    const extra = run.env || {};
    const env = Object.assign({}, process.env, extra, {
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `${WRY_ARGS} --remote-debugging-port=${run.port}`,
      WEBVIEW2_USER_DATA_FOLDER: run.data,
    });
    if (run.wslenv) {
      env.WSLENV = [process.env.WSLENV, 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS/w', 'WEBVIEW2_USER_DATA_FOLDER/w']
        .concat(Object.keys(extra).map((k) => k + '/w')).filter(Boolean).join(':');
    }
    exited = null;
    child = spawn(run.exe, [], { env, stdio: 'ignore', windowsHide: false });
    child.on('error', (e) => { exited = 'the app could not start: ' + String(e && e.message || e); });
    child.on('exit', (code) => { if (exited === null) exited = `the app exited (code ${code})`; });
    const limit = Date.now() + (run.timeout_sec || 90) * 1000;
    let last = '';
    while (Date.now() < limit) {
      // started from WSL, the WSL process that holds it may end at once: only a failure to start counts
      if (exited && /could not start/.test(exited)) throw new Error(exited);
      try {
        const browser = await chromium.connectOverCDP(cdp, { timeout: 5000 });
        const context = browser.contexts()[0];
        while (Date.now() < limit) {
          const page = context && appPage(context);
          if (page) {
            await page.waitForLoadState('load').catch(() => {});
            return { browser, context, page };
          }
          await sleep(300);
        }
        last = 'the app opened no page';
        await browser.close().catch(() => {});
      } catch (e) {
        last = String(e && e.message || e).split('\n')[0];
        await sleep(700);
      }
    }
    const err = new Error(`the app did not open WebView2's DevTools port ${run.port} within ${run.timeout_sec || 90}s` +
                          (exited ? ` (${exited})` : '') + (last ? `: ${last}` : ''));
    // the app started and ended by itself (a panic, an error at start): the code's; a port that never opened while the
    // app ran is this machine's (WebView2, Windows)
    err.appExited = Boolean(exited) && !/could not start/.test(exited);
    throw err;
  };
  let s;
  try {
    s = await start();
  } catch (e) {
    stopAll();
    if (way) way.close();
    throw e;
  }
  return {
    get browser() { return s.browser; },
    get context() { return s.context; },
    get page() { return s.page; },
    async restart() {   // the app closed and opened again: its data (WebView2's and the Rust side's) kept
      await s.browser.close().catch(() => {});
      stopAll();
      await sleep(1000);
      s = await start();
      return s.page;
    },
    async stop() {
      await s.browser.close().catch(() => {});
      stopAll();
      if (way) way.close();
    },
  };
}

// t.ipc of the real app: what the page asked the Rust side, from its requests to http://ipc.localhost/<command>.
function ipcOfApp(report, getPage) {
  const log = ipcLog(report, 'app');
  return {
    log,
    mocked: false,
    handle(cmd) {
      report.ipc.notes = report.ipc.notes || [];
      const n = `t.ipc.handle('${cmd}') is ignored in the real app: its Rust side answers (guard it with t.ipc.mocked)`;
      if (!report.ipc.notes.includes(n)) report.ipc.notes.push(n);
      return false;
    },
    calls: (cmd) => callsOf(log.seen, cmd),
    async emit(event, payload) {   // an event every listener of the app receives, sent from the page
      await getPage().evaluate(([e, p]) => window.__TAURI_INTERNALS__.invoke('plugin:event|emit', { event: e, payload: p }),
                               [event, payload === undefined ? null : payload]);
    },
    channel() { throw new Error('t.ipc.channel: the real app\'s channels are fed by its Rust side'); },
    state: {},
  };
}

// The real app's IPC: each request of the page to http://ipc.localhost/<command> handed to add(call).
function recordIpc(page, add) {
  page.on('request', (r) => {
    const u = r.url();
    if (!/^(http:\/\/ipc\.localhost|ipc:\/\/localhost)\//.test(u) || r.method() === 'OPTIONS') return;
    const cmd = decodeURIComponent(u.replace(/^(http:\/\/ipc\.localhost|ipc:\/\/localhost)\//, '').split('?')[0]);
    let args = r.postData();
    if (args && args.length > MAX_ARGS) args = args.slice(0, MAX_ARGS) + '…';
    const call = { cmd, args, url: page.url(), answered_by: 'rust' };
    add(call);
    r.response().then((res) => { if (res) call.status = res.status(); }).catch(() => {});
  });
}

// One app of its own, for a test of the auto test (or anything that is not fx's gate): a run folder made next to the
// others, the build copied there, a free DevTools port, then launchApp. spec: { exe (the build, a WSL path), runs (the
// folder of runs on Windows, a WSL path), tunnel (node.exe, while WSL's network is NAT), env, timeout_sec }.
async function openApp(spec, chromium) {
  const path = require('path');
  const crypto = require('crypto');
  const { execFileSync } = require('child_process');
  const win = (p) => execFileSync('wslpath', ['-w', p]).toString().trim();
  const dir = path.join(spec.runs, crypto.randomBytes(6).toString('hex'));
  fs.mkdirSync(path.join(dir, 'webview'), { recursive: true });
  const exe = path.join(dir, path.basename(spec.exe).replace(/\.exe$/i, '').replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 40) + '-app.exe');
  fs.copyFileSync(spec.exe, exe);
  const data = win(path.join(dir, 'webview'));
  const run = { exe, port: freePort(), data, wslenv: true, tunnel: spec.tunnel || null, env: spec.env || {},
                timeout_sec: spec.timeout_sec || 90, stop_ps: psEncoded(stopScript(win(exe), data)) };
  const removeDir = async () => {
    for (let i = 0; i < 10; i += 1) {   // Windows lets go of the files a moment after the processes end
      try { fs.rmSync(dir, { recursive: true, force: true }); return; } catch (e) { await sleep(1000); }
    }
  };
  let s;
  try {
    s = await launchApp(run, chromium);
  } catch (e) {
    await removeDir();
    throw e;
  }
  return Object.assign(s, { dir, async close() { await s.stop(); await removeDir(); } });
}

function psEncoded(script) {
  return Buffer.from("$ProgressPreference = 'SilentlyContinue'\n" + script, 'utf16le').toString('base64');
}

function stopScript(exeWin, dataWin) {   // as desktop.py's stop_script: this run's app and its WebView2, nothing else
  const q = (s) => "'" + String(s).replace(/'/g, "''") + "'";
  return `$exe = ${q(exeWin)}; $data = ${q(dataWin)}\n` +
    'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe -or ' +
    '($_.CommandLine -and $_.CommandLine.Contains($data)) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n';
}

// a DevTools port nothing listens on, here or on Windows
function freePort() {
  const { execFileSync } = require('child_process');
  let busy = '';
  try { busy = execFileSync('netstat.exe', ['-an', '-p', 'TCP'], { cwd: '/mnt/c', timeout: 30000 }).toString(); } catch (e) { busy = ''; }
  for (let i = 0; i < 200; i += 1) {
    const p = 20000 + Math.floor(Math.random() * 20000);
    if (!new RegExp(`:${p}\\s`).test(busy)) return p;
  }
  return 20000 + Math.floor(Math.random() * 20000);
}

const isIpcUrl = (u) => /^(http:\/\/(ipc|asset)\.localhost\/|ipc:\/\/localhost\/)/.test(String(u || ''));

module.exports = { attach, pageScript, builtin, launchApp, openApp, ipcOfApp, recordIpc, isIpcUrl, stopScript, psEncoded };

// node factory-desktop.js --page-script <setup.json>: what the page gets, for a person to read
if (require.main === module && process.argv[2] === '--page-script') {
  process.stdout.write(pageScript(JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))) + '\n');
}
