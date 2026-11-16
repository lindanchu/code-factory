"""Paths, settings, small file helpers, the journal and the masking of secrets."""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time

SETTINGS = "autotest.json"
STATE_DIR = ".autotest"
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

EXIT_USAGE = 2      # the command was called wrongly, or a question for the user must be answered first
EXIT_HUMAN = 3      # the environment needs a human (no settings, no key, the web cannot be reached)
EXIT_JIRA = 4
EXIT_GATE = 5       # a gate of the flow refused: nothing was sent, nothing is lost
EXIT_AGENT = 6      # an agent's result file is missing or invalid
EXIT_WAIT = 75      # nothing failed: the step waits for room on this machine

CODE_RE = re.compile(r"^(WEB|MOB|DES)-[A-Z]{3}-\d{2}$")
PLAN_KEY_RE = re.compile(r"^(WEB|MOB|DES)-[A-Z]{3}-\d{2}(-[A-Za-z0-9]+)?$")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")


class TxError(Exception):
    def __init__(self, message, code=1, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def out(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    sys.stdout.flush()


def hint(text):
    sys.stderr.write(text.rstrip() + "\n")
    sys.stderr.flush()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return dt.date.today().isoformat()


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def read_json(path, default=None):
    text = read(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        raise TxError("%s is not valid JSON: %s" % (path, e), code=EXIT_HUMAN)


def write_atomic(path, text):
    """Write through a temporary file and a rename: a reader never sees half a file, and a file another program holds
    open keeps its old content."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tx-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deep_merge(base, over):
    res = dict(base)
    for k, v in (over or {}).items():
        res[k] = deep_merge(res[k], v) if isinstance(v, dict) and isinstance(res.get(k), dict) else v
    return res


DEFAULTS = {
    "version": 1,
    "project": {"name": "", "jira_site": "", "jira_project": "", "root_tickets": []},
    "language": "vi",
    "platform": "WEB",
    "web": {"dir": "web", "base_url": "", "ignore_tls": False},
    "run": {
        # The project's own runner: tx gives it the results folder, the workers and the Playwright arguments.
        "command": "web/run.sh",
        "project_pattern": "{browser}-{width}",
        "retries": 1,
        # null: decided from the machine each time a run starts (never a number kept from another machine)
        "workers": None,
        # seconds between the starts of two runs of this repository (a product that cannot take two set-ups at once)
        "stagger_sec": 0,
        # other environments of the product: name -> {"env": {...}, "tests": "<folder under tests/>"}. With "own": true
        # it is the tester's alone; with "up"/"down" (or "sandbox": true, to use the main sandbox's commands) tx raises
        # it like the main sandbox; without either it is a deployed environment other people use too.
        "stacks": {},
        "env": {},
        "wait_sec": 1500,
    },
    "hub": {"url": "", "key_file": "", "state_file": STATE_DIR + "/hub-state.json", "ticket_url": "", "device_os": "",
            "upload_rewrite": {}, "targets": {},
            # the Hub's public guide for AI clients: whole addresses, or paths on the Hub's address
            "docs": ["/docs/ai-guide.md", "/docs/openapi.yaml"]},
    "version_probe": {"url": "", "pattern": r"v[0-9]+\.[0-9]+(\.[0-9]+)?", "ignore_tls": False},
    # Where the web under test runs. "sandbox": tx raises a build of the product on this machine for the tests and
    # takes it down afterwards. "own": an environment of the tester's own that is already running somewhere.
    # "shared": the project's test system; other people use it too and nothing can be wiped, so a case that needs an
    # environment of the tester's own (an empty system, the clock, system-wide settings) does not run there.
    "environment": {"kind": ""},
    # How a build is raised here and removed again: commands of the people who deploy the product, run from this
    # repository's root. tx fills {build}, {name} and {port}, and gives the same as AUTOTEST_BUILD, AUTOTEST_SANDBOX
    # (also COMPOSE_PROJECT_NAME) and AUTOTEST_PORT. `up` returns once the build was started; running it again while
    # it is up changes nothing. `health`: an address (or a path on `url`) that answers when it is ready, or
    # "cmd:<command>" that succeeds then. `build_id`: a command that prints what exactly runs (a commit, an image).
    "sandbox": {"up": "", "down": "", "url": "", "health": "", "build": "", "build_id": "", "start_timeout_sec": 600,
                "up_timeout_sec": 3600},
    # What every piece of data a test creates carries in its name, so it can be told apart and swept later.
    "data": {"mark": "qa-auto"},
    "design": {"dir": "design-src", "current": ""},
    "look": {"model": "opus", "batch": 8},
    "models": {"reader": "opus", "designer": "opus", "gaps": "sonnet", "explorer": "sonnet", "writer": "opus",
               "look": "opus", "triage": "opus", "verifier": "opus", "change": "opus", "wording": "sonnet",
               "hub": "opus", "floor": "sonnet"},
    # bugs: "draft" (the user looks once, then the tickets are created) or "create"; email + token_file: only to attach
    # the screenshot of a defect to its ticket (the Jira tools of Claude cannot upload a file)
    "jira": {"questions": "", "bugs": "", "label": "autotest-cho-chot", "bug_label": "autotest-loi", "email": "",
             "token_file": "", "api_url": "", "security_label": "autotest-baomat", "security_level": ""},
    # The full ZAP scan of a build (conventions.zap): how long it may attack, how long it crawls first, the image, and
    # when the session cannot be taken from a saved one (.auth/zap-*.json), a command that prints the login header.
    "zap": {"minutes": 60, "spider_minutes": 5, "image": "", "auth_command": "", "auth_header": "Authorization"},
    "git": {"commit": ""},
    "conventions": {},
    "asked": {},
}


class Repo:
    """One project's test repository: the folder that holds autotest.json."""

    def __init__(self, start=None, require=True):
        self.root = self._find(start or os.getcwd())
        self.cfg = None
        if self.root:
            own = read_json(os.path.join(self.root, SETTINGS), {})
            if not isinstance(own, dict):
                raise TxError("%s must hold a JSON object" % SETTINGS, code=EXIT_HUMAN)
            self.own = own
            self.cfg = deep_merge(DEFAULTS, own)
        elif require:
            raise TxError("this folder is not set up for auto test yet (no %s here or above)" % SETTINGS,
                          code=EXIT_HUMAN, next="init")
        else:
            self.root = os.path.abspath(start or os.getcwd())
            self.own = {}

    @staticmethod
    def _find(start):
        d = os.path.abspath(start)
        while True:
            if os.path.isfile(os.path.join(d, SETTINGS)):
                return d
            up = os.path.dirname(d)
            if up == d:
                return None
            d = up

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def web(self, *parts):
        return os.path.join(self.root, (self.cfg or DEFAULTS)["web"]["dir"], *parts)

    def state(self, *parts):
        return os.path.join(self.root, STATE_DIR, *parts)

    def c(self, dotted, default=None):
        cur = self.cfg
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def save(self, change):
        """Merge `change` into the settings file. Only tx writes it."""
        self.own = deep_merge(self.own, change)
        self.own.setdefault("_doc", "Written by the auto test skill (tx). Do not edit by hand: tell the skill what to "
                                    "change, it writes it here.")
        write_json(os.path.join(self.root, SETTINGS), self.own)
        self.cfg = deep_merge(DEFAULTS, self.own)

    def journal(self, event, **data):
        """What happened in the flow and cannot be read back from the files: runs, looks, pushes, ticket changes."""
        os.makedirs(self.state(), exist_ok=True)
        line = json.dumps(dict({"at": now_iso(), "event": event}, **data), ensure_ascii=False)
        with open(self.state("journal.jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def events(self, event=None, **match):
        rows = []
        for line in (read(self.state("journal.jsonl"), "") or "").split("\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if event and row.get("event") != event:
                continue
            if all(row.get(k) == v for k, v in match.items()):
                rows.append(row)
        return rows

    def features(self):
        """feature code -> {key, name, root, sort, folder}; folder is the test folder under tests/ (the code in lower
        case unless the file says otherwise)."""
        data = read_json(self.web("features.json"), {}) or {}
        res = {}
        for code, f in data.items():
            if not isinstance(f, dict):
                continue
            res[code] = dict(f, folder=f.get("folder") or code.lower())
        return res

    def language(self):
        return (self.c("language") or "vi").lower()


def record_comparer_change(repo, states, why, old="", new=""):
    """Results measured before the comparer changed are not sent for the design states the change affects ("*": all)."""
    path = repo.state("comparer.json")
    data = read_json(path, {"changes": []}) or {"changes": []}
    row = {"at": now_iso(), "states": list(states), "why": why, "from": old or "", "to": new or ""}
    data.setdefault("changes", []).append(row)
    write_json(path, data)
    repo.journal("comparer-changed", **row)
    return row


# ---------------------------------------------------------------- secrets

ANSI = re.compile(r"\x1b\[[0-9;]*m")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,2}")
APIKEY = re.compile(r"\b\d+\|[A-Za-z0-9]{20,}\b")
# name = value, name: value, "name": "value" (a plain word after the name is a sentence, not a secret: "mật khẩu sai")
KV = re.compile(r"(?i)\b(token|password|mat_khau|mật khẩu|passwd|secret|cookie|authorization|otp|api[_-]?key|"
                r"access[_-]?token|refresh[_-]?token)(\"?\s*[=:]\s*)(\"?)[^\s\",;]+")
BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
# a token carried on the path of a link (an invitation or reset link): /<word>/<long token>
PATH_TOKEN = re.compile(r"(/[a-z][a-z0-9-]{2,40}/)([A-Za-z0-9_-]{24,})(?![A-Za-z0-9_-])")
EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
MASK = "••••••••"


def mask(text, secrets=()):
    """Nothing the Hub stores may hold a password, a token, a cookie or a whole email address."""
    if text is None:
        return None
    t = ANSI.sub("", str(text))
    for s in secrets:
        if s:
            t = t.replace(s, MASK)
    t = APIKEY.sub(MASK, t)
    t = JWT.sub(MASK, t)
    t = BEARER.sub(lambda m: m.group(1) + m.group(2) + MASK, t)
    t = PATH_TOKEN.sub(lambda m: m.group(1) + MASK, t)
    t = KV.sub(lambda m: "%s%s%s%s" % (m.group(1), m.group(2), m.group(3), MASK), t)
    t = EMAIL.sub(lambda m: "%s***@%s" % (m.group(1), m.group(2)), t)
    return t


def has_secret(text):
    """A value that looks like a real secret (for the check of test data written into the plans)."""
    t = str(text or "")
    return bool(APIKEY.search(t) or JWT.search(t) or PATH_TOKEN.search(t))


def first_line(text):
    for line in ANSI.sub("", text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "x"


def parse_time(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def wait_until(path, seconds):
    """Leave at least `seconds` between two calls that pass the same file (two runs starting their set-up)."""
    if seconds <= 0:
        return 0.0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    waited = 0.0
    while True:
        try:
            fd = os.open(path + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                # the holder sleeps at most `seconds` with the lock: older than that, it was left by a tx that died
                if time.time() - os.path.getmtime(path + ".lock") > seconds * 2 + 120:
                    os.unlink(path + ".lock")
            except OSError:
                pass
            time.sleep(0.5)
            waited += 0.5
            continue
        try:
            os.close(fd)
            try:
                last = float(read(path, "0") or 0)
            except ValueError:
                last = 0.0
            left = last + seconds - time.time()
            if left > 0:
                time.sleep(left)
                waited += left
            write_atomic(path, "%f\n" % time.time())
            return waited
        finally:
            try:
                os.unlink(path + ".lock")
            except OSError:
                pass
