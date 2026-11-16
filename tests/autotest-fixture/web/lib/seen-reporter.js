// Fixture only. Listed after the runtime's reporter, it notes at the end of every attempt what is on disk at that very
// moment: whether the case file of that test already holds that attempt, and what run.json says. That is the proof
// that results are written as each test ends and not when the run is over (check.js reads seen.jsonl).
const fs = require('fs');
const path = require('path');

class SeenReporter {
  printsToStdio() { return false; }

  onBegin(config) {
    this.dir = path.resolve(path.dirname(config.configFile), process.env.RESULTS_DIR || 'results/latest');
  }

  onTestEnd(test, result) {
    const row = { id: test.id, title: test.title, retry: result.retry, status: result.status, file: null, attempts: 0, run: null };
    try {
      const cases = path.join(this.dir, 'cases');
      for (const name of fs.readdirSync(cases)) {
        if (!name.endsWith('.json')) continue;
        const c = JSON.parse(fs.readFileSync(path.join(cases, name), 'utf8'));
        if (c.title === test.title) { row.file = name; row.attempts = c.results.length; row.last = c.results[c.results.length - 1].status; }
      }
      row.run = JSON.parse(fs.readFileSync(path.join(this.dir, 'run.json'), 'utf8'));
    } catch (e) { row.problem = String(e.message || e); }
    fs.appendFileSync(path.join(this.dir, 'seen.jsonl'), JSON.stringify(row) + '\n');
  }
}

module.exports = SeenReporter;
