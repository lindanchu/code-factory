#!/bin/bash
# Proves the auto test runtime on a made-up product: copies the skill's runtime into this fixture's web/autotest/, runs
# the fixture's tests on Chromium (headless, one worker) and checks what they left behind (check.js).
#
#   NODE_PATH=<a node_modules folder that holds @playwright/test> tests/autotest-fixture/run.sh
#
# Four small runs, one after the other: the whole suite (8 tests; some fail on purpose); the one case with a heavy
# seeded difference again under two other choices of the project's owner (a BAD screen does not fail the case; no
# threshold chosen), each on a throw-away copy of this folder; and the edge paths of the UI check (edge/, 11 tests that
# assert for themselves). Then the saved session of an account shared by runs side by side (session-check.js, on a
# tiny web of its own on 127.0.0.1). No network, nothing installed.
set -euo pipefail

FIX=$(cd "$(dirname "$0")" && pwd)
RUNTIME=$(cd "$FIX/../../plugins/auto-factory/skills/test/runtime" && pwd)
CLI=$(cd "$FIX/web" && node -e "process.stdout.write(require.resolve('@playwright/test/cli'))" 2>/dev/null) || {
  echo "Node cannot find @playwright/test: set NODE_PATH to a node_modules folder that holds it" >&2
  exit 3
}

# the runtime as a project gets it: a copy the skill owns; and nothing left of an earlier run of this script
rm -rf "$FIX/web/autotest" "$FIX/web/results" "$FIX/edge/web/autotest" "$FIX/edge/web/results"
cp -R "$RUNTIME" "$FIX/web/autotest"
cp -R "$RUNTIME" "$FIX/edge/web/autotest"

SUFFIX=$(node -e "process.stdout.write(require('crypto').randomBytes(3).toString('hex'))")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/autotest-fixture.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

# play <test repository> <results folder under web/> [arguments of Playwright]
# Tests that fail on purpose make Playwright exit 1: check.js decides what is right.
play() {
  local root=$1 out=$2
  shift 2
  mkdir -p "$root/web/$out"
  (cd "$root/web" && RESULTS_DIR="$out" RUN_SUFFIX="$SUFFIX" node "$CLI" test --project=chromium-1440 --workers=1 "$@" \
    > "$root/web/$out/playwright.log" 2>&1) || true
}
# checked <what> <test repository> <results folder under web/>
checked() {
  node "$FIX/check.js" "$1" "$2" "$2/web/$3" || { echo "--- end of the Playwright log of this run:" >&2; tail -40 "$2/web/$3/playwright.log" >&2; exit 1; }
}
# other <name> <change of the settings c>: WEB-DEM-03 alone, on a copy of this folder with another choice
other() {
  local name=$1 edit=$2 root="$TMP/$1" part
  mkdir -p "$root/web"
  for part in autotest.json product design-src; do cp -R "$FIX/$part" "$root/"; done
  for part in autotest lib plans tests playwright.config.js; do cp -R "$FIX/web/$part" "$root/web/"; done
  node -e "const fs = require('fs'), f = process.argv[1], c = JSON.parse(fs.readFileSync(f, 'utf8')); $edit; fs.writeFileSync(f, JSON.stringify(c, null, 1) + '\n');" "$root/autotest.json"
  play "$root" results/run --grep "WEB-DEM-03"
  checked "$name" "$root" results/run
}

OUT="results/$(date +%Y%m%d-%H%M%S)-$SUFFIX"
play "$FIX" "$OUT"
checked main "$FIX" "$OUT"
other column "c.conventions.ui_bad.value = 'column'"
other unchosen "delete c.conventions.ui_threshold"

# the edge paths: every test must pass
mkdir -p "$FIX/edge/web/results/run"
(cd "$FIX/edge/web" && RESULTS_DIR=results/run node "$CLI" test --project=chromium-1440 --workers=1 > results/run/playwright.log 2>&1) || {
  echo "NOT AS EXPECTED (edge): a test of the edge paths failed" >&2
  tail -60 "$FIX/edge/web/results/run/playwright.log" >&2
  exit 1
}
echo "edge      $(grep -Eo '[0-9]+ passed' "$FIX/edge/web/results/run/playwright.log" | tail -1) on the paths the suite does not walk (a missing or extra dialog, a menu, the part a profile keeps, a broken design page, wrong calls)"

# the saved session of an account, shared by runs that work side by side (session-check.js, its own tiny web)
mkdir -p "$TMP/session/web" && cp -R "$RUNTIME" "$TMP/session/web/autotest"
(cd "$FIX/web" && node "$FIX/session-check.js" "$TMP/session")

echo "OK   results of the whole suite: tests/autotest-fixture/web/$OUT"
