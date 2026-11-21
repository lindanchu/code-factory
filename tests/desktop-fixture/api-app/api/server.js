// The app's API and its website, on one port: what app.up starts in the review's box.
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const page = fs.readFileSync(path.join(__dirname, '..', 'dist', 'index.html'));
http.createServer((q, r) => {
  // the real app's page is http://tauri.localhost: another origin
  r.setHeader('Access-Control-Allow-Origin', '*');
  if (q.url === '/health') return r.end('ok');
  if (q.url === '/api/greeting') {
    r.setHeader('Content-Type', 'application/json');
    return r.end(JSON.stringify({ text: 'hello from the API', host: os.hostname(), home: fs.existsSync(os.homedir()) }));
  }
  r.setHeader('Content-Type', 'text/html; charset=utf-8');
  r.end(page);
}).listen(Number(process.argv[2]), '127.0.0.1');
