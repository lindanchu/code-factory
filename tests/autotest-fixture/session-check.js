// Proves the runtime's session.js: runs that work side by side share the saved session of an account and never log
// each other out, on a tiny web that keeps one session per account (a new login ends the one before).
//
//   node session-check.js <a folder holding a copy of the runtime as web/autotest>
//
// Each run is a process of its own with its own browser, as the parts of a round and the trial runs of a writer are.
// Exit 0 when every round is as expected; else it says which one is not.
const http = require('http');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const [, , mode, ...rest] = process.argv;

// ---- one run: take the session of an account, then open a context from its file and look at a private page
async function child(name) {
  const { chromium } = require('@playwright/test');
  const { session, sessionFile } = require(path.join(process.env.WEB, 'autotest', 'session.js'));
  const browser = await chromium.launch();
  try {
    const check = async (page) => { await page.goto('/private'); return page.url().endsWith('/private'); };
    const login = async (page) => { await page.goto(`/login?u=${name}`); };
    const file = await session(browser, name, { login, check });
    if (file !== sessionFile(name)) throw new Error(`file ${file} is not sessionFile(${name})`);
    // what a test does: a context from the saved file, never a login of its own
    const ctx = await browser.newContext({ baseURL: process.env.BASE_URL, storageState: file });
    const page = await ctx.newPage();
    await page.goto('/private');
    process.stdout.write(page.url().endsWith('/private') ? 'in' : 'out');
  } finally {
    await browser.close();
  }
}

if (mode === 'child') {
  child(rest[0]).catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(2); });
} else {
  main().catch((e) => { console.error(`NOT AS EXPECTED (session): ${e.message}`); process.exit(1); });
}

// ---- the web: one session per account, a slow login (so runs that start together all find no session)
function web() {
  const current = {};
  const logins = {};
  let n = 0;
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    const sid = (/(?:^|;\s*)sid=([^;]+)/.exec(req.headers.cookie || '') || [])[1];
    const who = Object.keys(current).find((a) => current[a] === sid);
    if (u.pathname === '/login') {
      const acc = u.searchParams.get('u');
      logins[acc] = (logins[acc] || 0) + 1;
      current[acc] = `s${++n}`;
      setTimeout(() => {
        res.writeHead(302, { 'Set-Cookie': `sid=${current[acc]}; Path=/; HttpOnly`, Location: '/private' });
        res.end();
      }, 400);
    } else if (u.pathname === '/expire') {
      delete current[u.searchParams.get('u')];
      res.end('ok');
    } else if (u.pathname === '/private' && who) {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(`<p>${who}</p>`);
    } else if (u.pathname === '/private') {
      res.writeHead(302, { Location: '/login-page' });
      res.end();
    } else {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<p>login</p>');
    }
  });
  return { server, logins, current };
}

function runs(names, env) {
  return Promise.all(names.map((name) => new Promise((resolve) => {
    const p = spawn(process.execPath, [__filename, 'child', name], { env, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => resolve({ name, code, out, err }));
  })));
}

async function main() {
  const WEB = path.resolve(mode, 'web');
  const { sessionFile } = require(path.join(WEB, 'autotest', 'session.js'));
  const w = web();
  await new Promise((r) => w.server.listen(0, '127.0.0.1', r));
  const BASE_URL = `http://127.0.0.1:${w.server.address().port}`;
  const env = { ...process.env, WEB, BASE_URL };
  const expire = (acc) => new Promise((r) => http.get(`${BASE_URL}/expire?u=${acc}`, (res) => { res.resume(); res.on('end', r); }));
  const lines = [];
  const round = async (what, names, want) => {
    const got = await runs(names, env);
    const bad = got.filter((g) => g.code !== 0 || g.out !== 'in');
    if (bad.length) throw new Error(`${what}: ${bad.map((b) => `${b.name} ${b.code} ${b.out} ${b.err.slice(-400)}`).join(' | ')}`);
    for (const [acc, n] of Object.entries(want)) {
      if ((w.logins[acc] || 0) !== n) throw new Error(`${what}: ${acc} logged in ${w.logins[acc] || 0} times in all, expected ${n}`);
    }
    lines.push(`${what}: ${names.length} runs, all still logged in`);
  };
  try {
    if (sessionFile('admin', 'http://a.test') === sessionFile('admin', 'http://b.test')) throw new Error('two webs share one session file');
    await round('no saved session yet', ['admin', 'admin', 'admin', 'admin'], { admin: 1 });
    await round('a saved session that works', ['admin', 'admin', 'admin'], { admin: 1 });
    await expire('admin');
    await round('the saved session has expired', ['admin', 'admin', 'admin', 'admin'], { admin: 2 });
    await round('two accounts at once', ['admin', 'user1', 'admin', 'user1'], { admin: 2, user1: 1 });
    // a run that died while it held the lock: the next one takes the lock over instead of waiting for ever
    // (several runs find it at once: only one of them may take it over, three times over to give the race a chance)
    const lock = `${sessionFile('admin', BASE_URL)}.lock`;
    for (let i = 1; i <= 3; i++) {
      await expire('admin');
      fs.mkdirSync(lock);
      fs.writeFileSync(path.join(lock, 'owner'), `${require('os').hostname()} 999999`);
      await round(`a lock left by a run that died (${i})`, ['admin', 'admin', 'admin', 'admin', 'admin'], { admin: 2 + i });
      if (fs.existsSync(lock) || fs.existsSync(`${lock}.break`)) throw new Error('the lock was left behind');
    }
  } finally {
    w.server.close();
  }
  console.log(`session   ${lines.join('; ')}`);
}
