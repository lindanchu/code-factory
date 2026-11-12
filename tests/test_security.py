"""Security tests for fx (code-factory 1.0.0).

Standard library only. Run from the repository root:
    python3 -I -m unittest discover -s tests

Each test builds throw-away git repositories (a bare "project" origin, a factory repository, the job's clone) in
a temporary folder, and puts stub gitleaks / semgrep / osv-scanner / docker executables first on PATH. The stubs
record their argv, working folder and the credentials they can see, and print canned JSON.
"""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "code-factory", "skills", "factory")
FX_PATH = os.path.join(SKILL, "fx")
TEMPLATE = os.path.join(SKILL, "templates", "config.json")


def load_fx():
    sys.dont_write_bytecode = True  # fx has no .py suffix: keep __pycache__ out of the skill folder
    loader = importlib.machinery.SourceFileLoader("fx_under_test", FX_PATH)
    spec = importlib.util.spec_from_loader("fx_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fx = load_fx()

SECRET_ENV = {"JIRA_API_TOKEN": "jira-secret-value", "GH_TOKEN": "gh-secret", "GITHUB_TOKEN": "github-secret",
              "GITLAB_TOKEN": "gitlab-secret", "GLAB_TOKEN": "glab-secret", "CI_JOB_TOKEN": "ci-secret",
              "JIRA_EMAIL_X": "x", "ANTHROPIC_API_KEY": "anthropic-secret"}

STUB = r'''#!%(python)s -I
import json, os, re, sys, time
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
rec = {"tool": name, "argv": args, "cwd": os.getcwd(), "GIT_DIR": os.environ.get("GIT_DIR"),
       "secrets": sorted(k for k in os.environ if k in %(secret_names)r),
       "zap_auth": os.environ.get("ZAP_AUTH_HEADER_VALUE"), "zap_auth_site": os.environ.get("ZAP_AUTH_HEADER_SITE")}
if "--config" in args:
    try:
        rec["config_text"] = open(args[args.index("--config") + 1]).read()
    except OSError:
        rec["config_text"] = None
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
mode = os.environ.get("STUB_" + name.upper().replace("-", "_"), "")
time.sleep(float(os.environ.get("STUB_SLEEP_" + name.upper().replace("-", "_"), "0")))
if name == "gitleaks":
    if mode == "fail":
        print("gitleaks exploded")
        sys.exit(2)
    rpt = args[args.index("--report-path") + 1]
    report = os.environ.get("STUB_GITLEAKS_REPORT", "[]")
    find = os.environ.get("STUB_GITLEAKS_FIND")
    if os.environ.get("STUB_GITLEAKS_OLD") and "--max-archive-depth" in args:
        print("Error: unknown flag: --max-archive-depth")
        sys.exit(1)
    if find:  # really reads what it was given to scan: each file under the folder holding this text is a finding
        where = args[args.index("--source") + 1] if "--source" in args else args[1]
        depth = int(args[args.index("--max-archive-depth") + 1]) if "--max-archive-depth" in args else 0
        hits = []

        def look(label, data, left):
            if find.encode() in data:
                hits.append({"File": label, "RuleID": "stub-rule", "StartLine": 1, "Secret": "REDACTED", "Match": "REDACTED"})
            if left <= 0:
                return
            import io, tarfile, zipfile  # like gitleaks: an archive it cannot open is only passed over, in silence
            try:
                if label.endswith(".zip"):
                    z = zipfile.ZipFile(io.BytesIO(data))
                    for n in z.namelist():
                        look(label + "!" + n, z.read(n), left - 1)
                elif label.endswith((".tar", ".tgz", ".tar.gz")):
                    t = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
                    for m in t:
                        if m.isfile():
                            look(label + "!" + m.name, t.extractfile(m).read(), left - 1)
            except Exception:
                pass

        for folder, _, names in os.walk(where):
            for n in names:
                path = os.path.join(folder, n)
                look(path, open(path, "rb").read(), depth)
        report = json.dumps(hits)
    with open(rpt, "w") as f:
        f.write(report)
elif name == "semgrep":
    if mode == "old" and "--x-ignore-semgrepignore-files" in args:
        sys.stderr.write("Error: No such option: --x-ignore-semgrepignore-files\n")
        sys.exit(2)
    print(os.environ.get("STUB_SEMGREP_OUT", json.dumps({"results": [], "errors": []})))
elif name == "osv-scanner":
    if "--version" in args:
        print("osv-scanner version: %%s" %% os.environ.get("STUB_OSV_VERSION", "2.7.0"))
        sys.exit(0)
    if os.environ.get("STUB_OSV_VERSION", "2").startswith("1."):
        sys.stderr.write("flag provided but not defined: -all-packages\\n")
        sys.exit(127)
    if mode == "nosources":
        sys.stderr.write("No package sources found, --help for usage information.\n")
        sys.exit(128)
    sys.stderr.write(os.environ.get("STUB_OSV_STDERR", ""))
    base = any("fx-osv-base-" in a for a in args)  # the base branch's tree: clean unless a test says otherwise
    # By default it reads every lockfile that says nothing against it, and finds nothing wrong.
    read = []
    for folder, _, names in os.walk(args[-1] if os.path.isdir(args[-1]) else "."):
        for n in names:
            path = os.path.join(folder, n)
            text = open(path, errors="replace").read() if n.endswith((".lock", "-lock.json", "-lock.yaml", ".lockfile")) else "unreadable"
            if "unreadable" not in text:  # pkg:name in a test's lockfile: a package it lists
                read.append({"source": {"path": path}, "packages": [{"package": {"name": p, "version": "1"}}
                                                                    for p in ["ok"] + re.findall(r"pkg:(\S+)", text)]})
    print(os.environ.get("STUB_OSV_BASE_OUT" if base else "STUB_OSV_OUT", json.dumps({"results": read})))
elif name == "docker":
    if args[:1] == ["run"] and mode != "crash":
        for i, a in enumerate(args):
            if a == "-v":
                host = args[i + 1].split(":/zap/wrk")[0]
                report = args[args.index("-J") + 1] if "-J" in args else "zap.json"
                with open(os.path.join(host, report), "w") as f:
                    f.write(os.environ.get("STUB_ZAP_REPORT", json.dumps({"site": []})))
                print(os.environ.get("STUB_ZAP_LOG", ""))
    elif mode == "down" and args[:1] == ["info"]:
        sys.exit(1)
    elif args[:2] == ["image", "inspect"]:
        if args[-1] == os.environ.get("STUB_ZAP_IMAGE_GONE"):
            sys.exit(1)
        print(os.environ.get("STUB_ZAP_IMAGE_ID", "sha256:stubimage"))
    elif args[:2] == ["network", "inspect"]:
        print("172.17.0.1")
'''

TOOLS = ("gitleaks", "semgrep", "osv-scanner", "docker")


@contextlib.contextmanager
def not_on_this_machine(*names):
    """fx cannot find these tools, even where the machine running the tests has them in /usr/bin."""
    real = shutil.which
    with mock.patch.object(fx.shutil, "which", lambda n, *a, **k: None if n in names else real(n, *a, **k)):
        yield


def sh(cmd, cwd, check=True):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode != 0:
        raise AssertionError("%s failed: %s" % (cmd, p.stdout))
    return p.stdout


def slurp(path):
    with open(path) as f:
        return f.read()


def load_json(path):
    return json.loads(slurp(path))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Factory:
    """A project origin, a factory repository and one job (T-1) with its clone."""

    def __init__(self, root, cfg=None, tools=TOOLS, base_files=None):
        self.root = root
        self.bin = os.path.join(root, "bin")
        os.makedirs(self.bin)
        for t in tools:
            p = os.path.join(self.bin, t)
            write(p, STUB % {"python": sys.executable, "secret_names": sorted(SECRET_ENV)})
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for real in ("git", "bash", "tar"):
            w = shutil.which(real)
            if w and not os.path.exists(os.path.join(self.bin, real)):
                os.symlink(w, os.path.join(self.bin, real))
        self.log = os.path.join(root, "stub.log")
        self.out = os.path.join(root, "out")
        os.makedirs(self.out)
        # the project
        seed = os.path.join(root, "seed")
        os.makedirs(seed)
        sh(["git", "init", "-q", "-b", "main"], seed)
        for rel, text in (base_files or {"README.md": "project\n"}).items():
            write(os.path.join(seed, rel), text)
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "base"], seed)
        self.origin = os.path.join(root, "origin.git")
        sh(["git", "clone", "-q", "--bare", seed, self.origin], root)
        # the factory repository
        self.factory = os.path.join(root, "proj-factory")
        os.makedirs(self.factory)
        conf = {"project": {"url": self.origin, "name": "proj"}, "base_branch": "main", "max_rounds": 3}
        for k, v in (cfg or {}).items():
            conf[k] = v
        write(os.path.join(self.factory, "factory.json"), json.dumps(conf, indent=1))
        write(os.path.join(self.factory, "checks.sh"), 'env > "$FX_TEST_OUT/checks.env"\nexit 0\n')
        # the job's clone
        self.wt = os.path.join(root, "proj-factory-work", "T-1")
        sh(["git", "clone", "-q", self.origin, self.wt], root)
        self.branch = "factory/T-1-test"
        sh(["git", "checkout", "-q", "-b", self.branch, "origin/main"], self.wt)
        self.jd = os.path.join(self.factory, "jobs", "T-1")
        self.save_job()

    def env(self, **extra):
        e = dict(os.environ)
        e.update(SECRET_ENV)
        e.update(PATH=self.bin + os.pathsep + "/usr/bin:/bin", STUB_LOG=self.log, FX_TEST_OUT=self.out,
                 GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                 GIT_COMMITTER_EMAIL="t@example.com")
        e.update(extra)
        return e

    def save_job(self, **changes):
        job = {"id": "T-1", "issue_key": "T-1", "jira_base": None, "issue_url": None, "summary": "test job",
               "branch": self.branch, "worktree": self.wt, "base_branch": "main", "base_ref": "origin/main",
               "slot": 0, "port": 5100, "stage": "build", "round": 0, "round_start": 0, "max_rounds": 3,
               "risk": "high", "surfaces": [], "models": {"security": "opus", "code": "sonnet"},
               "reviewers": ["security", "code"], "reviews": {}, "gate": None, "flags": [], "decision": None,
               "needs_human": False, "incomplete": False, "handoff_reason": None, "setup_done": True,
               "history": []}
        if os.path.exists(os.path.join(self.jd, "job.json")):
            with open(os.path.join(self.jd, "job.json")) as f:
                job = json.load(f)
        job.update(changes)
        write(os.path.join(self.jd, "job.json"), json.dumps(job, indent=1))
        return job

    def job(self):
        with open(os.path.join(self.jd, "job.json")) as f:
            return json.load(f)

    def commit(self, files, msg="change"):
        for rel, text in files.items():
            write(os.path.join(self.wt, rel), text)
        sh(["git", "add", "--"] + list(files), self.wt)  # only these: the clone also holds local files
        sh(["git", "commit", "-q", "-m", msg], self.wt)

    def calls(self, tool=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            recs = [json.loads(l) for l in f if l.strip()]
        return [r for r in recs if tool is None or r["tool"] == tool]

    def ctx(self):
        cwd = os.getcwd()
        os.chdir(self.factory)
        try:
            return fx.Ctx()
        finally:
            os.chdir(cwd)


class FxTestCase(unittest.TestCase):
    tools = TOOLS
    cfg = None
    base_files = None

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-sec-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        gitcfg = os.path.join(self.tmp, "gitconfig")
        write(gitcfg, "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        base = {"GIT_CONFIG_GLOBAL": gitcfg, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "FACTORY_STATE_DIR": os.path.join(self.tmp, "state"),  # fx writes its board here, not in ~/.cache
                "FACTORY_TOOLS_DIR": os.path.join(self.tmp, "tools"),  # a machine's installed tools are not on PATH
                "FACTORY_WORKSPACE": os.path.join(self.tmp, "machine", "workspace.json")}  # nor its keys or workspace
        p = mock.patch.dict(os.environ, base)
        p.start()
        self.addCleanup(p.stop)
        self.f = Factory(self.tmp, cfg=self.cfg, tools=self.tools, base_files=self.base_files)
        p = mock.patch.dict(os.environ, self.f.env(), clear=True)
        p.start()
        self.addCleanup(p.stop)

    def fx(self, *argv):
        """Run an fx command in-process; returns (exit code, parsed JSON output)."""
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                fx.main(["--repo", self.f.factory] + list(argv))
            except SystemExit as e:
                code = e.code
        os.chdir(self.cwd)
        return code, json.loads(buf.getvalue())

    def gate(self):
        return self.fx("gate", "T-1")

    def checks_md(self, n=1):
        with open(os.path.join(self.f.jd, "round-%d" % n, "checks.md")) as f:
            return f.read()


# --------------------------------------------------------------------------
# 1. agent-controlled scanner suppression

class ScannerSuppressionTest(FxTestCase):
    def test_gitleaks_runs_with_factory_rules_outside_the_clone(self):
        self.f.commit({"app.py": "x = 1\n", ".gitleaks.toml": "[allowlist]\npaths = ['.*']\n",
                       ".gitleaksignore": "abc:app.py:rule:1\n"})
        code, res = self.gate()
        call = self.f.calls("gitleaks")[0]
        argv = call["argv"]
        self.assertIn("--ignore-gitleaks-allow", argv)
        ign = argv[argv.index("--gitleaks-ignore-path") + 1]
        self.assertFalse(ign.startswith(self.f.wt), "the ignore path must not be in the clone")
        self.assertNotIn("--config", argv)  # gitleaks' built-in default rules
        self.assertFalse(argv[-1].startswith(self.f.wt), "gitleaks must not scan the clone's folder")
        self.assertFalse(call["cwd"].startswith(os.path.realpath(self.f.wt)))
        self.assertEqual(os.path.realpath(call["GIT_DIR"]), os.path.realpath(os.path.join(self.f.wt, ".git")))
        # the settings files are flagged for a human, and their entries block as suppressions
        self.assertIn(".gitleaks.toml", res["flags"])
        self.assertIn(".gitleaksignore", res["flags"])
        self.assertTrue(any("scanner settings changed" in n for n in res["notes"]))
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("Scanner settings changed", self.checks_md())

    def test_gitleaks_config_from_the_factory_repository(self):
        write(os.path.join(self.f.factory, "gitleaks.toml"), "[extend]\nuseDefault = true\n")
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"gitleaks_config": "gitleaks.toml"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        argv = self.f.calls("gitleaks")[0]["argv"]
        self.assertEqual(argv[argv.index("--config") + 1], os.path.join(self.f.factory, "gitleaks.toml"))
        self.assertEqual(res["gate"], "PASS", res)

    def test_semgrep_disables_nosem_and_semgrepignore(self):
        self.f.commit({"app.py": "x = 1\n"})
        self.gate()
        argv = self.f.calls("semgrep")[0]["argv"]
        self.assertIn("--disable-nosem", argv)
        self.assertIn("--x-ignore-semgrepignore-files", argv)

    def test_semgrep_without_the_ignore_option_notes_the_semgrepignore(self):
        os.environ["STUB_SEMGREP"] = "old"
        self.f.commit({"app.py": "x = 1\n", ".semgrepignore": "app.py\n"})
        code, res = self.gate()
        runs = self.f.calls("semgrep")
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--x-ignore-semgrepignore-files", runs[1]["argv"])
        self.assertIn("--disable-nosem", runs[1]["argv"])
        self.assertTrue(any(".semgrepignore" in n and "cannot ignore" in n for n in res["notes"]), res["notes"])

    def test_osv_uses_its_own_config(self):
        self.f.commit({"app.py": "x = 1\n",
                       "osv-scanner.toml": "[[IgnoredVulns]]\nid = \"GHSA-xxxx\"\nreason = \"later\"\n"})
        code, res = self.gate()
        calls = self.f.calls("osv-scanner")
        self.assertTrue(calls)
        for c in calls:
            cfg = c["argv"][c["argv"].index("--config") + 1]
            self.assertFalse(cfg.startswith(self.f.wt))
            self.assertNotIn("IgnoredVulns", c["config_text"] or "")
        self.assertIn("osv-scanner.toml", res["flags"])

    def test_suppression_comments_block_unless_allowed(self):
        self.f.commit({"app.py": "eval(x)  # nosemgrep\n", "lib.js": "const k = 1 // gitleaks:allow\n",
                       "osv-scanner.toml": "[[IgnoredVulns]]\nid = \"GHSA-1\"\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        with open(os.path.join(self.f.jd, "round-1", "security.md")) as f:
            sec = f.read()
        self.assertIn("nosemgrep", sec)
        self.assertIn("gitleaks:allow", sec)
        self.assertIn("osv-scanner.toml", sec)
        self.assertIn("security scan: 3 blocking", self.checks_md())

    def test_suppressions_allowed_by_the_factory(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"allow_suppressions": True}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"app.py": "eval(x)  # nosem\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_scan_suppressions_unit(self):
        self.f.commit({"a.py": "ok = 1\nbad = 2  # nosemgrep: rule\n", ".gitleaksignore": "# comment\nfp1\nfp2\n"})
        ctx = self.f.ctx()
        mb = sh(["git", "merge-base", "origin/main", "HEAD"], self.f.wt).strip()
        found = fx.scan_suppressions(ctx, self.f.job(), mb)
        locs = sorted(x["location"] for x in found)
        self.assertEqual(locs, [".gitleaksignore:2", "a.py:2"])
        self.assertTrue(all(x["suppression"] for x in found))
        self.assertIn("2 entries", [x for x in found if x["rule"] == ".gitleaksignore"][0]["message"])

    def test_template_marks_scanner_settings_sensitive(self):
        tpl = load_json(TEMPLATE)
        for p in (".gitleaks.toml", ".gitleaksignore", ".semgrepignore", "osv-scanner.toml",
                  "sub/.gitleaksignore", "sub/osv-scanner.toml"):
            self.assertTrue(fx.match_any(p, tpl["sensitive_paths"]), p)


# --------------------------------------------------------------------------
# 2. tokens never reach project or agent code

class EnvStripTest(FxTestCase):
    cfg = {"setup": 'env > "$FX_TEST_OUT/setup.env"'}

    def assert_clean(self, env_text):
        names = {l.split("=", 1)[0] for l in env_text.splitlines() if "=" in l}
        for k in SECRET_ENV:
            self.assertNotIn(k, names)
        self.assertIn("FX_TEST_OUT", names)

    def test_project_env_helper(self):
        ctx = self.f.ctx()
        env = fx.project_env(ctx, EXTRA="1")
        for k in SECRET_ENV:
            self.assertNotIn(k, env)
        self.assertEqual(env["EXTRA"], "1")
        self.assertIn("PATH", env)
        env, _ = fx.app_env(ctx, self.f.job())
        self.assertNotIn("JIRA_API_TOKEN", env)
        self.assertNotIn("GH_TOKEN", fx.checks_env(ctx, self.f.job()))
        # the factory may let one through, or strip more
        ctx.cfg["security"] = {"pass_env": ["GITHUB_TOKEN"], "strip_env": ["AWS_*"]}
        os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
        env = fx.project_env(ctx)
        self.assertIn("GITHUB_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        # only what project code gets is stripped: fx's own environment is as it was
        self.assertEqual(os.environ["JIRA_API_TOKEN"], "jira-secret-value")

    def test_checks_setup_seed_and_scanners_get_no_tokens(self):
        self.f.save_job(setup_done=False)
        code, res = self.fx("setup", "T-1")
        self.assertEqual(code, 0, res)
        self.assert_clean(slurp(os.path.join(self.f.out, "setup.env")))
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assert_clean(slurp(os.path.join(self.f.out, "checks.env")))
        for c in self.f.calls():
            self.assertEqual(c["secrets"], [], c["tool"])
        seed = os.path.join(self.tmp, "seed.sh")
        write(seed, 'env > "$FX_TEST_OUT/seed.env"\n')
        self.assertTrue(fx.run_seed(self.f.ctx(), self.f.job(), seed, os.path.join(self.tmp, "seedout")))
        self.assert_clean(slurp(os.path.join(self.f.out, "seed.env")))

    def test_ui_check_script_gets_no_tokens(self):
        ctx = self.f.ctx()
        captured = {}

        def fake_run(cmd, cwd=None, check=True, capture=True, env=None, timeout=None, input_text=None):
            captured["env"] = env
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(fx, "playwright_ok", return_value=(True, "ok")), \
                mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "run", side_effect=fake_run):
            fx.ui_run(ctx, self.f.job(), os.path.join(self.tmp, "check.js"), os.path.join(self.tmp, "ui"))
        for k in SECRET_ENV:
            self.assertNotIn(k, captured["env"])
        self.assertIn("BASE_URL", captured["env"])


# --------------------------------------------------------------------------
# 3. a missing security block means the template defaults

class FactorySettingsTest(FxTestCase):
    """The gate's settings are what is committed and pushed, whatever git has been told about them."""
    TAMPERED = 'echo tampered > "$FX_TEST_OUT/tampered"\nexit 0\n'

    def setUp(self):
        super().setUp()
        self.fac = self.f.factory
        write(os.path.join(self.fac, ".gitignore"), "jobs/\nlocal/\nbaseline.json\n")
        self.add_settings()
        sh(["git", "init", "-q", "-b", "main"], self.fac)
        self.commit_all("factory")
        self.f.commit({"app.py": "x = 1\n"})

    def add_settings(self):
        pass

    def commit_all(self, msg):
        sh(["git", "add", "-A"], self.fac)
        sh(["git", "commit", "-q", "-m", msg], self.fac)

    def with_upstream(self):
        self.remote = os.path.join(self.tmp, "factory.git")
        sh(["git", "clone", "-q", "--bare", self.fac, self.remote], self.tmp)
        sh(["git", "remote", "add", "origin", self.remote], self.fac)
        sh(["git", "fetch", "-q", "origin"], self.fac)
        sh(["git", "branch", "-q", "--set-upstream-to=origin/main", "main"], self.fac)

    def tamper_checks(self):
        write(os.path.join(self.fac, "checks.sh"), self.TAMPERED)

    def tamper_config(self):
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        cfg["security"] = {"block_severity": "critical", "required": []}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))

    def assert_refused(self, because=""):
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn(because, res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.f.out, "tampered")))
        self.assertEqual(self.f.job()["round"], 0)

    def test_a_plain_change_is_refused(self):
        self.tamper_checks()
        self.assert_refused("checks.sh")

    def test_a_committed_factory_runs(self):
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_hidden_from_git(self):
        for how in ("--assume-unchanged", "--skip-worktree"):
            for name, tamper in (("checks.sh", self.tamper_checks), ("factory.json", self.tamper_config)):
                with self.subTest(how=how, name=name):
                    sh(["git", "update-index", how, name], self.fac)
                    tamper()
                    self.assert_refused("differs from what is committed: %s" % name)
                    sh(["git", "update-index", how.replace("--", "--no-"), name], self.fac)
                    sh(["git", "checkout", "-q", "--", name], self.fac)

    def test_telling_git_not_to_look_is_enough_to_stop_it(self):
        sh(["git", "update-index", "--assume-unchanged", "checks.sh"], self.fac)
        self.assert_refused("git is told not to look at checks.sh")

    def test_a_clean_filter_cannot_vouch_for_a_changed_file(self):
        blob = sh(["git", "rev-parse", "HEAD:checks.sh"], self.fac).strip()
        sh(["git", "config", "filter.x.clean", "git cat-file blob %s" % blob], self.fac)
        write(os.path.join(self.fac, ".git", "info", "attributes"), "checks.sh filter=x\n")
        size = os.path.getsize(os.path.join(self.fac, "checks.sh"))
        write(os.path.join(self.fac, "checks.sh"), "exit 0 #".ljust(size - 1, "x") + "\n")  # same size: git asks the filter
        self.assertEqual(sh(["git", "status", "--porcelain", "--", "checks.sh"], self.fac), "", "git itself sees nothing")
        self.assert_refused("differs from what is committed: checks.sh")

    def test_a_symlink_is_not_the_file(self):
        other = os.path.join(self.tmp, "elsewhere.sh")
        write(other, slurp(os.path.join(self.fac, "checks.sh")))
        os.remove(os.path.join(self.fac, "checks.sh"))
        os.symlink(other, os.path.join(self.fac, "checks.sh"))
        sh(["git", "update-index", "--assume-unchanged", "checks.sh"], self.fac)
        self.assert_refused("differs from what is committed: checks.sh")

    def crlf_checkout(self, name):
        sh(["git", "config", "core.autocrlf", "true"], self.fac)
        path = os.path.join(self.fac, name)
        os.remove(path)
        sh(["git", "checkout", "-q", "--", name], self.fac)
        with open(path, "rb") as f:
            data = f.read()
        if b"\r\n" not in data:
            with open(path, "wb") as f:
                f.write(data.replace(b"\n", b"\r\n"))
            sh(["git", "add", name], self.fac)

    def test_a_windows_checkout_of_the_settings_is_no_change(self):
        self.crlf_checkout("factory.json")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")

    def test_but_carriage_returns_change_what_a_script_does(self):
        """They break `set -e` and `false`, and git status says nothing with core.autocrlf on."""
        self.crlf_checkout("checks.sh")
        self.assertEqual(sh(["git", "status", "--porcelain", "--", "checks.sh"], self.fac).strip(), "")
        self.assert_refused("differs from what is committed: checks.sh")

    def test_a_path_written_with_dot_slash(self):
        write(os.path.join(self.fac, "own.toml"), "x = 1\n")
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        cfg["security"] = {"gitleaks_config": "./own.toml"}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))
        self.commit_all("settings")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")
        write(os.path.join(self.fac, "own.toml"), "x = 2\n")
        self.assertIn("own.toml", fx.factory_dirty(self.f.ctx()))

    def test_a_path_outside_the_repository_is_not_asked_about(self):
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        write(os.path.join(self.tmp, "shared.yml"), "rules: []\n")
        cfg["security"] = {"semgrep_config": "../shared.yml"}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))
        self.commit_all("settings")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")

    def test_rules_in_a_submodule(self):
        team = os.path.join(self.tmp, "team-rules")
        os.makedirs(team)
        sh(["git", "init", "-q", "-b", "main"], team)
        write(os.path.join(team, "own.yml"), "rules: []\n")
        sh(["git", "add", "-A"], team)
        sh(["git", "commit", "-q", "-m", "r"], team)
        sh(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", team, "shared-rules"], self.fac)
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        cfg["security"] = {"semgrep_config": "shared-rules"}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))
        self.commit_all("settings")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")
        write(os.path.join(self.fac, "shared-rules", "own.yml"), "rules: [{}]\n")
        self.assertIn("shared-rules", fx.factory_dirty(self.f.ctx()))

    def test_a_link_to_a_name_that_is_not_utf8(self):
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        os.makedirs(os.path.join(self.fac, "own-rules"))
        write(os.path.join(self.fac, "own-rules", "a.yml"), "rules: []\n")
        os.symlink(os.fsdecode(b"caf\xe9"), os.path.join(self.fac, "own-rules", "link.yml"))
        cfg["security"] = {"semgrep_config": "own-rules"}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))
        self.commit_all("settings")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")

    def commit_a_change_of_settings(self):
        self.with_upstream()
        self.tamper_checks()
        self.tamper_config()
        self.commit_all("tune")

    def test_commits_only_on_this_machine(self):
        self.commit_a_change_of_settings()
        self.assert_refused("commits only on this machine")

    def test_with_the_upstream_unset(self):
        self.commit_a_change_of_settings()
        sh(["git", "branch", "--unset-upstream"], self.fac)
        self.assert_refused("commits only on this machine")

    def test_with_the_remote_ref_moved_to_it(self):
        self.commit_a_change_of_settings()
        sh(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], self.fac)
        self.assert_refused("commits only on this machine")

    def test_with_a_remote_ref_made_up_for_it(self):
        self.commit_a_change_of_settings()
        sh(["git", "update-ref", "refs/remotes/origin/made-up", "HEAD"], self.fac)
        self.assert_refused("commits only on this machine")

    def test_on_a_branch_of_its_own(self):
        self.commit_a_change_of_settings()
        sh(["git", "checkout", "-q", "-b", "tune"], self.fac)
        self.assert_refused("commits only on this machine")

    def test_on_a_detached_head(self):
        self.commit_a_change_of_settings()
        sh(["git", "checkout", "-q", "--detach"], self.fac)
        self.assert_refused("commits only on this machine")

    def test_the_claude_folder_counts_too(self):
        self.with_upstream()
        write(os.path.join(self.fac, ".claude", "settings.json"), "{}\n")
        self.commit_all("settings")
        self.assert_refused("commits only on this machine")

    def test_a_repository_whose_first_commit_is_not_pushed(self):
        remote = os.path.join(self.tmp, "empty.git")
        sh(["git", "init", "-q", "--bare", remote], self.tmp)
        sh(["git", "remote", "add", "origin", remote], self.fac)
        self.assert_refused("commits only on this machine")

    def test_a_remote_out_of_reach_goes_by_what_was_last_heard(self):
        self.with_upstream()
        os.rename(self.remote, self.remote + ".gone")
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_pushed_they_count(self):
        self.with_upstream()
        self.tamper_config()
        self.commit_all("tune")
        sh(["git", "push", "-q", "origin", "main"], self.fac)
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_other_commits_of_this_machine_do_not_matter(self):
        self.with_upstream()
        write(os.path.join(self.fac, "knowledge", "stack.md"), "# stack\n")
        self.commit_all("notes")
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_behind_the_upstream_is_not_a_local_change(self):
        self.with_upstream()
        other = os.path.join(self.tmp, "teammate")
        sh(["git", "clone", "-q", self.remote, other], self.tmp)
        write(os.path.join(other, "checks.sh"), "exit 0\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-q", "-m", "teammate"], other)
        sh(["git", "push", "-q", "origin", "main"], other)
        sh(["git", "fetch", "-q", "origin"], self.fac)
        self.assertEqual(fx.factory_unpushed(self.f.ctx()), "")

    def test_doctor_says_the_same(self):
        self.with_upstream()
        self.tamper_checks()
        self.commit_all("tune")
        checks = {c["check"]: c for c in self.fx("doctor")[1]["checks"]}
        self.assertFalse(checks["the gate's settings pushed"]["ok"])
        self.assertIn("checks.sh", checks["the gate's settings pushed"]["detail"])

    def test_doctor_notes_a_repository_with_no_remote(self):
        checks = {c["check"]: c for c in self.fx("doctor")[1]["checks"]}
        self.assertTrue(checks["factory repository has a remote (advice)"]["ok"])
        self.assertIn("no remote", checks["factory repository has a remote (advice)"]["detail"])

    def test_what_checks_runs_cannot_commit_them_unnoticed(self):
        write(os.path.join(self.fac, "checks.sh"),
              'cd "%s"\necho "# later" >> factory.json\ngit add factory.json\ngit commit -q -m tune\nexit 0\n' % self.fac)
        self.commit_all("checks that commit")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertIn("changed while it ran", self.checks_md())
        self.assertIn("factory.json", self.checks_md())

    def test_what_checks_runs_cannot_change_them_unnoticed(self):
        write(os.path.join(self.fac, "checks.sh"),
              'echo "# later" >> "%s/factory.json"\nexit 0\n' % self.fac)
        self.commit_all("checks that reach out")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertIn("changed while it ran", self.checks_md())


class FactoryRulesTest(FactorySettingsTest):
    """Scanner rules kept in the factory repository count as settings."""

    def add_settings(self):
        write(os.path.join(self.fac, "rules", "own.yml"), "rules: []\n")
        write(os.path.join(self.fac, "gitleaks.toml"), "title = 'x'\n")
        write(os.path.join(self.fac, "osv.toml"), "# x\n")
        cfg = load_json(os.path.join(self.fac, "factory.json"))
        cfg["security"] = {"semgrep_config": "rules", "gitleaks_config": "gitleaks.toml", "osv_config": "osv.toml"}
        write(os.path.join(self.fac, "factory.json"), json.dumps(cfg))

    def test_a_rule_changed_behind_gits_back(self):
        sh(["git", "update-index", "--assume-unchanged", "rules/own.yml"], self.fac)
        write(os.path.join(self.fac, "rules", "own.yml"), "rules: [{}]\n")
        self.assert_refused("rules/own.yml")

    def test_a_new_rule_that_is_not_committed(self):
        write(os.path.join(self.fac, "rules", "more.yml"), "rules: []\n")
        self.assert_refused("not committed: rules/more.yml")

    def test_what_semgrep_would_not_load_is_nobodys_business(self):
        write(os.path.join(self.fac, "rules", ".DS_Store"), "finder\n")
        write(os.path.join(self.fac, "rules", "notes.txt"), "x\n")
        self.assertEqual(fx.factory_dirty(self.f.ctx()), "")

    def test_a_name_decomposed_on_disk_is_the_same_name(self):
        nfc, nfd = unicodedata.normalize("NFC", "caf\u00e9.yml"), unicodedata.normalize("NFD", "caf\u00e9.yml")
        write(os.path.join(self.fac, "rules", nfc), "rules: []\n")
        self.commit_all("accent")
        if nfd not in os.listdir(os.path.join(self.fac, "rules")):
            os.rename(os.path.join(self.fac, "rules", nfc), os.path.join(self.fac, "rules", nfd))
        self.assertNotIn("not committed", fx.factory_dirty(self.f.ctx()))

    def test_the_other_scanners_settings(self):
        for name in ("gitleaks.toml", "osv.toml"):
            with self.subTest(name=name):
                sh(["git", "update-index", "--skip-worktree", name], self.fac)
                write(os.path.join(self.fac, name), "changed = 1\n")
                self.assert_refused(name)
                sh(["git", "update-index", "--no-skip-worktree", name], self.fac)
                sh(["git", "checkout", "-q", "--", name], self.fac)


class HistoryRewritesTest(FxTestCase):
    """A clone never brings refs/replace or grafts, and with them what fx looks at is not what gets pushed."""

    def replace_something(self, root):
        head = sh(["git", "rev-parse", "HEAD"], root).strip()
        sh(["git", "replace", head, sh(["git", "commit-tree", "HEAD^{tree}", "-m", "other"], root).strip()], root)

    def assert_stopped(self, because):
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn(because, res["error"])
        self.assertEqual(self.f.job()["round"], 0)
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn(because, res["error"])

    def test_refs_replace_in_the_clone(self):
        self.f.commit({"app.py": "x = 1\n"})
        self.replace_something(self.f.wt)
        self.assert_stopped("refs/replace/")

    def test_refs_replace_in_the_factory_repository(self):
        sh(["git", "init", "-q", "-b", "main"], self.f.factory)
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\n")
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "factory"], self.f.factory)
        self.replace_something(self.f.factory)
        self.f.commit({"app.py": "x = 1\n"})
        self.assert_stopped("the factory repository has refs/replace/")

    def test_a_grafts_file_makes_the_branch_look_merged(self):
        self.f.commit({"app.py": "x = 1\n"})
        base = sh(["git", "rev-parse", "origin/main"], self.f.wt).strip()
        head = sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()
        write(os.path.join(self.f.wt, ".git", "info", "grafts"), "%s %s\n" % (base, head))
        self.assert_stopped("info/grafts")

    def test_a_plain_clone_has_neither(self):
        self.f.commit({"app.py": "x = 1\n"})
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_a_shallow_boundary_hides_history_too(self):
        self.f.commit({"app.py": "x = 1\n"})
        head = sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()
        write(os.path.join(self.f.wt, ".git", "shallow"), head + "\n")
        self.assert_stopped(".git/shallow")

    def test_a_shallow_factory_repository_is_fine(self):
        sh(["git", "init", "-q", "-b", "main"], self.f.factory)
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\n")
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "factory"], self.f.factory)
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory).strip()
        write(os.path.join(self.f.factory, ".git", "shallow"), head + "\n")
        self.f.commit({"app.py": "x = 1\n"})
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())


class SomethingElseOnThePortTest(FxTestCase):
    """A server left over from an earlier run answered in the app's place, and ZAP read that: nothing found, gate PASS."""
    cfg = {"app": {"up": "sleep 30"}, "security": {"required": [], "scanners": {"zap": False, "headers": False}}}

    def test_it_is_not_taken_for_the_app(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            self.f.save_job(port=other.getsockname()[1])
            self.f.commit({"app.py": "x = 1\n"})
            code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("already answers", self.checks_md())
        self.assertFalse(os.path.exists(fx.app_state_path(self.f.ctx(), self.f.job())))

    def test_a_free_port_is_tried_as_usual(self):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        self.f.save_job(port=port)
        self.f.commit({"app.py": "x = 1\n"})
        with mock.patch.object(fx, "http_up", return_value=True), mock.patch.object(fx, "app_stop"):
            info = fx.app_start(self.f.ctx(), self.f.job())
        self.assertEqual(info["reused"], False)
        os.killpg(load_json(fx.app_state_path(self.f.ctx(), self.f.job()))["pid"], 15)


APP_SERVER = r'''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


class AppUpThatExitsTest(FxTestCase):
    """app.up may start the app and exit, as `docker compose up -d` does: the app is still the one fx started."""

    def test_a_later_fx_finds_it_running(self):
        server = os.path.join(self.tmp, "server.py")
        write(server, APP_SERVER)
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        self.f.save_job(port=port)
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["app"] = {"up": "(nohup %s %s {port} >/dev/null 2>&1 &)" % (sys.executable, server), "health": "/health",
                      "down": "pkill -f '%s {port}' ; true" % server, "start_timeout_sec": 15}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.addCleanup(subprocess.run, ["pkill", "-f", "%s %d" % (server, port)])
        ctx, job = self.f.ctx(), self.f.job()
        self.assertFalse(fx.app_start(ctx, job)["reused"])
        self.assertTrue(load_json(fx.app_state_path(ctx, job))["detached"])
        # app.up's shell has exited, so its pid says nothing: without the mark the port would look taken by a stranger
        self.assertTrue(fx.app_start(ctx, job)["reused"])
        fx.app_stop(ctx, job)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    break
            time.sleep(0.1)
        else:
            self.fail("the app still answers after app.down")


class WhatTheGateMeasuresFromTest(FxTestCase):
    """The ref a job is measured from, and the branch it pushes, are not for the clone to say."""

    def semgrep_finds_it(self):
        report = json.dumps({"errors": [], "results": [{"check_id": "sqli", "path": "pay.py", "start": {"line": 1},
                                                        "extra": {"severity": "ERROR", "message": "m", "metadata": {}}}]})
        return mock.patch.dict(os.environ, STUB_SEMGREP_OUT=report)

    def base_arg(self):
        argv = self.f.calls("semgrep")[-1]["argv"]
        return argv[argv.index("--baseline-commit") + 1]

    def test_moving_the_base_ref_back_changes_nothing(self):
        origin_main = sh(["git", "rev-parse", "origin/main"], self.f.wt).strip()
        self.f.commit({"pay.py": "eval(x)\n"})
        self.f.commit({"other.py": "x = 1\n"})
        sh(["git", "update-ref", "refs/remotes/origin/main", "HEAD~1"], self.f.wt)  # so pay.py looks old
        with self.semgrep_finds_it():
            res = self.gate()[1]
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertEqual(self.base_arg(), origin_main)
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.f.wt).strip(), origin_main)

    def test_moving_it_is_undone_before_fx_pr_too(self):
        self.f.commit({"pay.py": "x = 1\n"})
        self.f.save_job(stage="pr", gate="PASS")
        with mock.patch.object(fx, "job_base_from_remote", wraps=fx.job_base_from_remote) as asked:
            self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(asked.call_count, 1)

    def test_the_clones_own_remote_is_not_asked(self):
        other = os.path.join(self.tmp, "other.git")
        sh(["git", "clone", "-q", "--bare", self.f.origin, other], self.tmp)
        self.f.commit({"pay.py": "x = 1\n"})
        sh(["git", "push", "-q", "--force", other, "HEAD:refs/heads/main"], self.f.wt)
        sh(["git", "remote", "set-url", "origin", other], self.f.wt)
        origin_main = sh(["git", "--git-dir", self.f.origin, "rev-parse", "refs/heads/main"], self.tmp).strip()
        self.gate()
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.f.wt).strip(), origin_main)

    def test_nobody_can_say_where_the_base_is(self):
        self.f.commit({"pay.py": "x = 1\n"})
        os.rename(self.f.origin, self.f.origin + ".gone")
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn("could not read the base branch main", res["error"])
        self.assertEqual(self.f.job()["round"], 0)

    def test_new_commits_on_the_base_do_not_hurt(self):
        other = os.path.join(self.tmp, "teammate")
        sh(["git", "clone", "-q", self.f.origin, other], self.tmp)
        write(os.path.join(other, "theirs.txt"), "x\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-q", "-m", "theirs"], other)
        sh(["git", "push", "-q", "origin", "main"], other)
        self.f.commit({"pay.py": "x = 1\n"})
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_head_elsewhere_is_not_what_gets_pushed(self):
        self.f.commit({"ok.py": "x = 1\n"})
        self.f.commit({"pay.py": "eval(x)\n"})
        sh(["git", "checkout", "-q", "--detach", "HEAD~1"], self.f.wt)
        res = self.gate()[1]
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("not on the job's branch", self.checks_md())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn("detached HEAD", res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")

    def test_another_branch_too(self):
        self.f.commit({"ok.py": "x = 1\n"})
        sh(["git", "checkout", "-q", "-b", "elsewhere"], self.f.wt)
        self.assertEqual(self.gate()[1]["gate"], "FAIL", self.checks_md())
        self.assertIn("on elsewhere, not on the job's branch", self.checks_md())

    def test_the_scanners_read_the_real_history_too(self):
        self.f.commit({"app.py": "x = 1\n"})
        self.gate()
        self.assertIn("GIT_NO_REPLACE_OBJECTS=1", slurp(os.path.join(self.f.out, "checks.env")))


class ConfigDefaultsTest(FxTestCase):
    def test_no_security_block_runs_every_scanner(self):
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        tools = {c["tool"] for c in self.f.calls()}
        self.assertTrue({"gitleaks", "semgrep", "osv-scanner"} <= tools, tools)
        self.assertTrue(os.path.exists(os.path.join(self.f.jd, "round-1", "security.md")))
        ctx = self.f.ctx()
        self.assertEqual(fx.scanners_on(ctx), {n: True for n in fx.SCANNERS})
        self.assertEqual(fx.required_scanners(ctx), ["gitleaks", "osv", "semgrep"])

    def test_explicit_off_is_listed_as_skipped(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"scanners": {"semgrep": False}, "required": []}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertFalse(self.f.calls("semgrep"))
        self.assertTrue(any(s.startswith("semgrep: turned off") for s in res["skipped"]), res["skipped"])
        self.assertEqual(self.f.job()["security_missed"], [])  # a human's choice, not a missed scan

    def test_pre_push_scan_without_security_block(self):
        self.f.commit({"app.py": "x = 1\n"})
        ctx = self.f.ctx()
        notes = fx.pr_secret_scan(ctx, self.f.job(), False)
        self.assertTrue(self.f.calls("gitleaks"))
        self.assertEqual(notes, ["secret scan before push: clean"])
        ctx.cfg["security"] = {"scanners": {"gitleaks": False}}
        notes = fx.pr_secret_scan(ctx, self.f.job(), False)
        self.assertIn("turned off", notes[0])


# --------------------------------------------------------------------------
# 4. failed or missing scanners, approver, built-in patterns

class RequiredScannersTest(FxTestCase):
    tools = ("semgrep", "osv-scanner")  # no gitleaks

    def test_missing_required_gitleaks_blocks(self):
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md()
        self.assertIn("required security scanner(s) did not run", md)
        self.assertIn("gitleaks", md)

    def test_not_required_runs_builtin_and_forces_a_human(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": []}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], ["gitleaks"])
        self.assertTrue(any("built-in check" in s for s in res["skipped"]))
        # the approver is told, and APPROVE is turned into needs-human
        code, ctxres = self.fx("ctx", "T-1", "--agent", "approver")
        self.assertIn("Security scanners skipped this round", ctxres["prompt"])
        self.f.save_job(stage="approve", flags=[])
        write(os.path.join(self.f.jd, "decision.md"), "APPROVE\n\nfine\n")
        code, r = self.fx("result", "T-1", "decision")
        self.assertEqual(code, 0, r)
        job = self.f.job()
        self.assertTrue(job["needs_human"])
        self.assertIn("security scanners skipped", job["handoff_reason"])
        self.assertIn("note", r)


class ZapWithoutDockerTest(FxTestCase):
    cfg = {"app": {"up": "true"}, "security": {"required": []}}

    def gate_with_app(self, **env):
        self.f.commit({"app.py": "x = %d\n" % self.f.job()["round"]})
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), \
                mock.patch.object(fx, "scan_headers", self.headers):
            return self.gate()

    @staticmethod
    def headers(ctx, job, url):
        return [], None

    def approve(self):
        self.f.save_job(stage="approve", flags=[])
        write(os.path.join(self.f.jd, "decision.md"), "APPROVE\n\nfine\n")
        code, res = self.fx("result", "T-1", "decision")
        self.assertEqual(code, 0, res)
        return res

    def set_required(self, *names):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": list(names)}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def assert_noted_only(self, res, why):
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any(s.startswith("zap: ") and why in s for s in res["skipped"]), res["skipped"])
        self.assertEqual(self.f.job()["security_missed"], [])
        self.assertIn(why, slurp(os.path.join(self.f.jd, "round-1", "security.md")))
        code, prompt = self.fx("ctx", "T-1", "--agent", "approver")
        self.assertIn("Security scanners skipped this round", prompt["prompt"])  # the approver still hears of it
        res = self.approve()
        self.assertNotIn("note", res)
        self.assertFalse(self.f.job()["needs_human"])

    def test_docker_not_running_is_only_noted(self):
        code, res = self.gate_with_app(STUB_DOCKER="down")
        self.assert_noted_only(res, "not running")
        self.assertFalse([c for c in self.f.calls("docker") if c["argv"][0] == "run"])

    def test_docker_not_installed_is_only_noted(self):
        with not_on_this_machine("docker"):
            code, res = self.gate_with_app()
            self.assert_noted_only(res, "not installed")

    def test_zap_failing_with_docker_up_still_needs_a_human(self):
        code, res = self.gate_with_app(STUB_DOCKER="crash")
        self.assertEqual(self.f.job()["security_missed"], ["zap"])
        self.assertTrue(any("ZAP produced no report" in s for s in res["skipped"]), res["skipped"])
        res = self.approve()
        self.assertIn("note", res)
        self.assertTrue(self.f.job()["needs_human"])

    def test_required_zap_without_docker_fails_the_gate_and_says_why(self):
        for name in ("zap", "docker"):  # docker is an alias of zap
            self.set_required(name)
            self.f.save_job(stage="build")
            code, res = self.gate_with_app(STUB_DOCKER="down")
            self.assertEqual(res["gate"], "FAIL")
            md = self.checks_md(self.f.job()["round"])
            self.assertIn("required security scanner(s) did not run", md)
            self.assertIn("not running", md)

    def test_other_scanners_still_count_as_missed(self):
        with not_on_this_machine("osv-scanner"):
            code, res = self.gate_with_app()
            self.assertEqual(self.f.job()["security_missed"], ["osv"])
            self.assertIn("note", self.approve())

    def test_headers_failing_still_counts_as_missed(self):
        self.headers = lambda ctx, job, url: ([], "the app did not answer")  # on the instance: never bound
        code, res = self.gate_with_app()
        self.assertEqual(self.f.job()["security_missed"], ["headers"])

    def test_zap_turned_off_never_asks_docker(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": [], "scanners": {"zap": False}}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.gate_with_app(STUB_DOCKER="down")
        self.assertEqual(self.f.job()["security_missed"], [])
        self.assertTrue(any("turned off" in s for s in res["skipped"]), res["skipped"])
        self.assertEqual(self.f.calls("docker"), [])


class RequiredToolsReportedTest(FxTestCase):
    tools = ("semgrep", "osv-scanner", "docker")  # no gitleaks

    def required_check(self, name="gitleaks"):
        code, res = self.fx("doctor")
        self.assertEqual(code, 0, res)
        found = [c for c in res["checks"] if c["check"] == "required scanner %s installed" % name]
        self.assertEqual(len(found), 1, res["checks"])
        return found[0], res

    def set_security(self, **security):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = security
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def install_need(self, name="gitleaks"):
        code, res = self.fx("install", "status")
        self.assertEqual(code, 0, res)
        return next(t["need"] for t in res["tools"] if t["name"] == name), res

    def test_doctor_fails_without_a_required_scanner(self):
        check, res = self.required_check()
        self.assertFalse(check["ok"])
        self.assertIn("remove it from security.required", check["detail"])
        self.assertFalse(res["ok"])
        self.assertFalse([c for c in res["checks"] if c["check"] == "tool gitleaks (optional)"])
        self.assertTrue([c for c in res["checks"] if c["check"] == "tool docker (optional)"])

    def test_doctor_passes_with_the_tool_on_path(self):
        write(os.path.join(self.f.bin, "gitleaks"), "#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.f.bin, "gitleaks"), 0o755)
        check, _ = self.required_check()
        self.assertTrue(check["ok"], check)

    def test_doctor_follows_security_required(self):
        self.set_security(required=[])
        code, res = self.fx("doctor")
        self.assertFalse([c for c in res["checks"] if c["check"].startswith("required scanner")])
        self.assertTrue([c for c in res["checks"] if c["check"] == "tool gitleaks (optional)"])

    def test_doctor_flags_a_required_scanner_turned_off(self):
        self.set_security(required=["semgrep"], scanners={"semgrep": False})
        check, _ = self.required_check("semgrep")
        self.assertFalse(check["ok"])
        self.assertIn("turned off", check["detail"])

    def test_doctor_flags_required_zap_without_a_running_docker(self):
        self.set_security(required=["docker"])  # an alias of zap
        with mock.patch.dict(os.environ, {"STUB_DOCKER": "down"}):
            check, _ = self.required_check("zap")
        self.assertFalse(check["ok"])
        self.assertIn("not running", check["detail"])
        check, _ = self.required_check("zap")
        self.assertTrue(check["ok"], check)

    def test_install_marks_the_tools_of_required_scanners(self):
        need, res = self.install_need()
        self.assertEqual(need, "required")
        self.assertFalse(res["ok"])  # a required tool is missing
        self.assertEqual(self.install_need("semgrep")[0], "required")
        self.set_security(required=["semgrep"])
        self.assertEqual(self.install_need("semgrep")[0], "required")
        self.assertEqual(self.install_need()[0], "recommended")
        self.set_security(required=["gitleaks"])
        self.assertEqual(self.install_need("semgrep")[0], "recommended")

    def test_install_outside_a_factory_uses_the_default(self):
        st = fx.machine_status(None)
        self.assertEqual(next(t["need"] for t in st["tools"] if t["name"] == "gitleaks"), "required")


class RequiredScannerErrorTest(FxTestCase):
    def test_failing_gitleaks_blocks(self):
        os.environ["STUB_GITLEAKS"] = "fail"
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("gitleaks failed", self.checks_md())

    def test_required_semgrep_and_unknown_name(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": ["gitleaks", "semgrep", "osv-scanner"]}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertEqual(fx.required_scanners(self.f.ctx()), ["gitleaks", "semgrep", "osv"])
        os.environ["STUB_SEMGREP_OUT"] = "not json"
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("semgrep: required", self.checks_md())

    def test_gitleaks_finding_blocks(self):
        os.environ["STUB_GITLEAKS_REPORT"] = json.dumps([{"RuleID": "aws-access-token", "File": "app.py",
                                                          "StartLine": 1, "Commit": "abcdef1234"}])
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("aws-access-token", slurp(os.path.join(self.f.jd, "round-1", "security.md")))


class BuiltinPatternTest(unittest.TestCase):
    def names(self, line, path="src/app.py"):
        return [n for n, _ in fx.secret_hits(line, path)]

    def test_new_key_formats(self):
        self.assertIn("anthropic-key", self.names("k = sk-ant-api03-" + "a1B2" * 20))
        self.assertIn("openai-key", self.names("OPENAI=sk-proj-" + "Ab3_" * 12))
        self.assertIn("openai-key", self.names("OPENAI=sk-" + "Ab3d" * 12))
        self.assertIn("azure-storage-key", self.names(
            "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + "A" * 86 + "==;"))
        self.assertIn("secret-assignment", self.names('db_password = "Xy7#kq92LmP"'))
        self.assertIn("secret-assignment", self.names("'api_key' => 'Zq81mmPw0aa'"))
        self.assertIn("secret-assignment", self.names('SECRET_KEY: "h8Gd72kslQ"'))

    def test_validation_rules_and_labels_are_not_secrets(self):
        for path, line in (
                ("app/Http/Requests/RegisterRequest.php", "'password' => 'required|string|min:8|confirmed',"),
                ("app/Http/Requests/ResetRequest.php", "'token' => 'required|string',"),
                ("app/Http/Controllers/Auth.php", "$request->validate(['token' => 'required|exists:password_resets,token']);"),
                ("app/Http/Requests/A.php", "'token' => 'required_with:email',"),
                ("app/Http/Requests/A.php", "'password' => 'nullable|string|strong_password',"),
                ("resources/js/locales/en.js", "password: 'Password',"),
                ("lang/fr/validation.php", "'password' => 'Mot_de_passe',")):
            self.assertEqual(self.names(line, path), [], line)

    def test_real_values_next_to_those_words_still_count(self):
        for line in ("'password' => 'Xy7kq2@ab1234',",
                     "'api_key' => 'f3c9a1e07b5d42c8961e0d7ab34f52c6',",
                     "'token' => '12|aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd',",  # a Laravel Sanctum token
                     "'password' => 'Hunt3r2|xYz9',",
                     "'password' => 'CorrectHorseBatteryStaple',",
                     "'api_key' => 'qwertyuiopasdfghjklzxcvbnmQWERTY',",
                     "'password' => '839201746',",
                     "'token' => 'admin:Hunt3r2pw',",
                     "'secret' => 'secret:Hunt3r2pw',"):
            self.assertIn("secret-assignment", self.names(line, "config/services.php"), line)
        both = self.names("'secret' => 'sk_" + "live_abcdefghijklmnopqrstuvwx',", "config/services.php")
        self.assertEqual(both, ["stripe-live-key", "secret-assignment"])

    def test_false_positive_guards(self):
        self.assertEqual(self.names('password = "Xy7#kq92LmP"', "tests/test_login.py"), [])
        self.assertEqual(self.names('password = "Xy7#kq92LmP"', "src/login.spec.ts"), [])
        self.assertEqual(self.names('password = "changeme123"'), [])
        self.assertEqual(self.names('password = "${DB_PASSWORD}"'), [])
        self.assertEqual(self.names("token = 'process.env.TOKEN'"), [])
        self.assertEqual(self.names("tokenizer = 'bert-base-uncased'"), [])
        self.assertEqual(self.names('password = "short"'), [])
        self.assertEqual(self.names("sk-ant is a prefix"), [])


class BuiltinSecretGateTest(FxTestCase):
    tools = ("semgrep", "osv-scanner")  # no gitleaks: the built-in check is what runs
    cfg = {"security": {"required": []}}

    def test_validation_rule_passes_the_gate(self):
        self.f.commit({"app/Http/Requests/RegisterRequest.php": "<?php\nreturn ['password' => 'required|string|min:8'];\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_real_password_fails_the_gate(self):
        self.f.commit({"config/database.php": "<?php\nreturn ['password' => 'Xy7kq2@ab1234'];\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")

    def test_new_code_may_name_the_site_but_not_the_password(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["copy_into_worktree"] = [".env"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        write(os.path.join(self.f.factory, "local", ".env"), LARAVEL_ENV)
        self.f.commit({"app/Site.php": "<?php\n$u = 'https://admin.acme-corp.com/x';\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.f.save_job(stage="build")
        self.f.commit({"app/Db.php": "<?php\n$p = getpw() ?: 'Xy7kq2@ab1234';\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")

    def test_outgoing_text_keeps_rules_and_redacts_tokens(self):
        ctx = self.f.ctx()
        rule = "Validation: 'password' => 'required|string|min:8'"
        self.assertEqual(fx.scrub_text(ctx, rule), (rule, []))
        text, hits = fx.scrub_text(ctx, "Use 'token' => '12|aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd' to log in")
        self.assertNotIn("aBcDeFgH", text)
        self.assertEqual(hits, ["secret-assignment"])


# --------------------------------------------------------------------------
# 5. fx never commits local secret files

class LocalFilesNotCommittedTest(FxTestCase):
    base_files = {"README.md": "project\n", ".env": "APP_ENV=local\n", "config/app.txt": "a\n",
                  ".env.example": "APP_ENV=\n", "sub/.env.sample": "APP_ENV=\n", "old-name.txt": "x\n", "gone.txt": "x\n"}
    cfg = {"copy_into_worktree": [".env"], "security": {"required": []}}

    def setUp(self):
        super().setUp()
        write(os.path.join(self.f.factory, "local", ".env"), "APP_ENV=local\nDB_PASSWORD=Zx81-real-pw\n")
        fx.copy_local_files(self.f.ctx(), self.f.wt)  # what job_clone does

    def test_wip_commit_leaves_out_env_files(self):
        self.f.commit({"src/done.txt": "done\n"}, "work")
        write(os.path.join(self.f.wt, ".env.local"), "TOKEN=abc\n")
        write(os.path.join(self.f.wt, "sub", ".env.production"), "TOKEN=abc\n")
        write(os.path.join(self.f.wt, "src", "new.txt"), "new\n")
        write(os.path.join(self.f.wt, "config", "app.txt"), "changed\n")
        self.f.save_job(stage="pr", incomplete=True, needs_human=True, handoff_reason="test")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        tree = sh(["git", "ls-tree", "-r", "--name-only", self.f.branch], self.f.origin).split()
        self.assertIn("src/new.txt", tree)
        self.assertIn("config/app.txt", tree)
        self.assertNotIn(".env.local", tree)
        self.assertNotIn("sub/.env.production", tree)
        self.assertEqual(sh(["git", "show", "%s:.env" % self.f.branch], self.f.origin), "APP_ENV=local\n")
        self.assertTrue(any("local files not committed" in n for n in res["notes"]), res["notes"])

    def test_is_local_file(self):
        ctx = self.f.ctx()
        for path in (".env", ".env.local", ".env.production", "sub/.env", "sub/.env.staging", ".envrc", ".env-prod",
                     ".env_backup", ".env.example.local", ".env.prod.bak"):
            self.assertTrue(fx.is_local_file(ctx, path), path)
        for path in (".env.example", ".env.sample", ".env.dist", ".env.template", "sub/.env.example", "app.env", "readme.md"):
            self.assertFalse(fx.is_local_file(ctx, path), path)

    def test_copy_into_worktree_wins_over_the_shared_names(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["copy_into_worktree"] = [".env.example"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertTrue(fx.is_local_file(self.f.ctx(), ".env.example"))

    def test_wip_commit_keeps_shared_env_files_and_every_kind_of_change(self):
        self.f.commit({"src/done.txt": "done\n"}, "work")
        write(os.path.join(self.f.wt, ".env.example"), "APP_ENV=\nNEW_SETTING=\n")
        write(os.path.join(self.f.wt, "sub", ".env.sample"), "APP_ENV=\nNEW_SETTING=\n")
        write(os.path.join(self.f.wt, ".env.template"), "APP_ENV=\n")
        write(os.path.join(self.f.wt, ".env.local"), "TOKEN=abc\n")
        write(os.path.join(self.f.wt, ".envrc"), "export TOKEN=abc\n")
        write(os.path.join(self.f.wt, "src", "caf\u00e9 [id].txt"), "new\n")
        sh(["git", "mv", "old-name.txt", "new-name.txt"], self.f.wt)
        os.remove(os.path.join(self.f.wt, "gone.txt"))
        self.f.save_job(stage="pr", incomplete=True, needs_human=True, handoff_reason="test")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        tree = sh(["git", "-c", "core.quotepath=off", "ls-tree", "-r", "--name-only", self.f.branch], self.f.origin).splitlines()
        self.assertIn(".env.template", tree)
        self.assertIn("src/caf\u00e9 [id].txt", tree)
        self.assertIn("new-name.txt", tree)
        self.assertNotIn("old-name.txt", tree)
        self.assertNotIn("gone.txt", tree)
        self.assertNotIn(".env.local", tree)
        self.assertNotIn(".envrc", tree)
        self.assertIn("NEW_SETTING", sh(["git", "show", "%s:.env.example" % self.f.branch], self.f.origin))
        self.assertIn("NEW_SETTING", sh(["git", "show", "%s:sub/.env.sample" % self.f.branch], self.f.origin))
        self.assertEqual(sh(["git", "show", "%s:.env" % self.f.branch], self.f.origin), "APP_ENV=local\n")
        note = next(n for n in res["notes"] if "local files not committed" in n)
        self.assertIn(".env.local", note)
        self.assertNotIn(".env.example", note)

    def test_a_local_file_a_builder_staged_stays_out(self):
        self.f.commit({"src/done.txt": "done\n"}, "work")
        write(os.path.join(self.f.wt, ".env.local"), "TOKEN=abc\n")
        sh(["git", "add", "-f", ".env.local"], self.f.wt)
        self.f.save_job(stage="pr", incomplete=True, needs_human=True, handoff_reason="test")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        tree = sh(["git", "ls-tree", "-r", "--name-only", self.f.branch], self.f.origin).split()
        self.assertNotIn(".env.local", tree)

    def test_copy_into_worktree_is_not_untracked_work(self):
        os.remove(os.path.join(self.f.wt, ".env"))
        sh(["git", "rm", "-q", "--cached", ".env"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "untrack"], self.f.wt)
        fx.copy_local_files(self.f.ctx(), self.f.wt)
        tracked, untracked = fx.worktree_dirt(self.f.ctx(), self.f.wt)
        self.assertEqual(untracked, [])

    def test_pick_leaves_out_env_files(self):
        cwt = os.path.join(self.tmp, "proj-factory-work", "T-1-c1")
        sh(["git", "clone", "-q", self.f.origin, cwt], self.tmp)
        sh(["git", "checkout", "-q", "-b", self.f.branch + "-c1", "origin/main"], cwt)
        fx.copy_local_files(self.f.ctx(), cwt)
        write(os.path.join(cwt, "feature.txt"), "x\n")
        write(os.path.join(cwt, ".env.production"), "SECRET=1\n")
        self.f.save_job(best_of=2, candidates=[{"k": 1, "worktree": cwt, "branch": self.f.branch + "-c1",
                                                 "port": 5103, "setup": True}])
        code, res = self.fx("pick", "T-1", "--k", "1")
        self.assertEqual(code, 0, res)
        tree = sh(["git", "ls-tree", "-r", "--name-only", "HEAD"], self.f.wt).split()
        self.assertIn("feature.txt", tree)
        self.assertNotIn(".env.production", tree)
        self.assertEqual(sh(["git", "show", "HEAD:.env"], self.f.wt), "APP_ENV=local\n")
        self.assertTrue(any("local files not committed" in n and ".env.production" in n for n in res["notes"]), res)

    def test_pick_keeps_shared_env_files(self):
        cwt = os.path.join(self.tmp, "proj-factory-work", "T-1-c1")
        sh(["git", "clone", "-q", self.f.origin, cwt], self.tmp)
        sh(["git", "checkout", "-q", "-b", self.f.branch + "-c1", "origin/main"], cwt)
        fx.copy_local_files(self.f.ctx(), cwt)
        write(os.path.join(cwt, ".env.example"), "APP_ENV=\nNEW_SETTING=\n")
        write(os.path.join(cwt, ".env.local"), "TOKEN=abc\n")
        write(os.path.join(cwt, ".envrc"), "export TOKEN=abc\n")
        write(os.path.join(cwt, "caf\u00e9.txt"), "x\n")
        self.f.save_job(best_of=2, candidates=[{"k": 1, "worktree": cwt, "branch": self.f.branch + "-c1",
                                                 "port": 5103, "setup": True}])
        code, res = self.fx("pick", "T-1", "--k", "1")
        self.assertEqual(code, 0, res)
        self.assertIn("NEW_SETTING", sh(["git", "show", "HEAD:.env.example"], self.f.wt))
        self.assertTrue(os.path.exists(os.path.join(self.f.wt, "caf\u00e9.txt")))
        tree = sh(["git", "ls-tree", "-r", "--name-only", "HEAD"], self.f.wt).split()
        self.assertNotIn(".env.local", tree)
        self.assertNotIn(".envrc", tree)
        self.assertEqual(sh(["git", "show", "HEAD:.env"], self.f.wt), "APP_ENV=local\n")
        note = next(n for n in res["notes"] if "local files not committed" in n)
        self.assertIn(".env.local", note)
        self.assertNotIn(".env.example", note)

    def test_pick_without_local_files_has_no_note(self):
        cwt = os.path.join(self.tmp, "proj-factory-work", "T-1-c1")
        sh(["git", "clone", "-q", self.f.origin, cwt], self.tmp)
        sh(["git", "checkout", "-q", "-b", self.f.branch + "-c1", "origin/main"], cwt)
        write(os.path.join(cwt, "feature.txt"), "x\n")
        self.f.save_job(best_of=2, candidates=[{"k": 1, "worktree": cwt, "branch": self.f.branch + "-c1",
                                                 "port": 5103, "setup": True}])
        code, res = self.fx("pick", "T-1", "--k", "1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["notes"], [])


# --------------------------------------------------------------------------
# 6. ZAP: digest pin, host-gateway, current uid

class ZapTest(FxTestCase):
    def run_zap(self, platform, reachable=True):
        ctx = self.f.ctx()
        rdir = os.path.join(self.tmp, "zap")
        notes = []
        conn = mock.MagicMock()
        side = None if reachable else OSError("refused")
        with mock.patch.object(fx.sys, "platform", platform), \
                mock.patch.object(fx.socket, "create_connection", return_value=conn, side_effect=side):
            f, err = fx.scan_zap(ctx, self.f.job(), "http://127.0.0.1:5100/", rdir, notes)
        self.assertIsNone(err)
        run = [c for c in self.f.calls("docker") if c["argv"][0] == "run"][-1]["argv"]
        return run, notes, rdir

    def test_linux_uses_host_gateway_and_uid(self):
        argv, notes, rdir = self.run_zap("linux")
        self.assertIn("--add-host", argv)
        self.assertEqual(argv[argv.index("--add-host") + 1], "host.docker.internal:host-gateway")
        self.assertNotIn("--network", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "http://host.docker.internal:5100/")
        self.assertEqual(argv[argv.index("--user") + 1], "%d:%d" % (os.getuid(), os.getgid()))
        self.assertNotEqual(stat.S_IMODE(os.stat(rdir).st_mode), 0o777)
        self.assertEqual(notes, [])

    def test_zap_gets_a_home_it_can_write_as_this_user(self):
        # Java ignores HOME and asks passwd, which has no entry for this uid in the image: ZAP then exits at once.
        argv, _, _ = self.run_zap("darwin")
        self.assertIn("JAVA_TOOL_OPTIONS=-Duser.home=/tmp/zap-user", argv)
        shell = argv[argv.index("-c") + 1]
        self.assertIn("cp -R /home/zap/.ZAP /tmp/zap-user/.ZAP", shell)
        self.assertIn('exec zap-baseline.py "$@"', shell)

    def test_linux_falls_back_to_host_network_with_a_note(self):
        argv, notes, _ = self.run_zap("linux", reachable=False)
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        self.assertEqual(argv[argv.index("-t") + 1], "http://127.0.0.1:5100/")
        self.assertTrue(notes and "host network" in notes[0])

    def test_mac_targets_host_docker_internal(self):
        argv, notes, _ = self.run_zap("darwin")
        self.assertNotIn("--network", argv)
        self.assertNotIn("--add-host", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "http://host.docker.internal:5100/")

    def test_image_pin_note(self):
        self.assertIn("moving tag", fx.zap_image_note("ghcr.io/zaproxy/zaproxy:stable"))
        self.assertIsNone(fx.zap_image_note("ghcr.io/zaproxy/zaproxy@sha256:" + "a" * 64))
        # and the gate's security run carries it as a note
        self.f.commit({"app.py": "x = 1\n"})
        with mock.patch.object(fx, "scan_headers", return_value=([], None)), \
                mock.patch.object(fx.sys, "platform", "darwin"):
            res = fx.run_security(self.f.ctx(), self.f.job(), os.path.join(self.tmp, "r"), "http://127.0.0.1:5100/")
        self.assertTrue(any("moving tag" in n for n in res["notes"]), res["notes"])


# --------------------------------------------------------------------------
# 7. local env values: only secrets

LARAVEL_ENV = """APP_NAME=Laravel
APP_KEY=base64:Q2hhbmdlTWVQbGVhc2VUaGlzSXNBVGVzdEtleTEyMzQ=
APP_URL=http://localhost:8000
DB_HOST=mariadb
DB_USERNAME=root
DB_PASSWORD=Xy7kq2@ab1234
SESSION_DRIVER=database
SESSION_DOMAIN=.acme-corp.com
SANCTUM_STATEFUL_DOMAINS=admin.acme-corp.com
MAIL_FROM_ADDRESS=no-reply@acme-corp.com
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_BUCKET=a2b-uploads-prod
VITE_PUSHER_APP_KEY="${PUSHER_APP_KEY}"
AUTH_GUARD=web
"""


class LocalSecretValuesTest(FxTestCase):
    cfg = {"copy_into_worktree": [".env"]}

    def test_only_secret_values(self):
        key = "q8Zr2LmX9vB4nT7wK1pD"
        write(os.path.join(self.f.factory, "local", ".env"), "\n".join([
            "APP_NAME=Laravel", "DB_HOST=mariadb", "DB_CONNECTION=mysql", "DB_DATABASE=factory_app",
            "DB_PASSWORD=Zx81-real-pw", "APP_KEY=base64:c2VjcmV0a2V5dmFsdWUxMjM0NTY=", "DB_USERNAME=root",
            "REDIS_PASSWORD=null", "SOME_ID=" + key, "MAIL_PASSWORD=factory", ""]))
        vals = fx.local_secret_values(self.f.ctx())
        self.assertEqual(vals, {"Zx81-real-pw", "base64:c2VjcmV0a2V5dmFsdWUxMjM0NTY=", key})

    def values(self, text):
        write(os.path.join(self.f.factory, "local", ".env"), text)
        return fx.local_secret_values(self.f.ctx())

    def test_laravel_env_keeps_secrets_and_drops_settings(self):
        vals = self.values(LARAVEL_ENV)
        self.assertEqual(vals, {"Xy7kq2@ab1234", "AKIAIOSFODNN7EXAMPLE",
                                "base64:Q2hhbmdlTWVQbGVhc2VUaGlzSXNBVGVzdEtleTEyMzQ="})

    def test_outgoing_text_keeps_domains_and_redacts_the_password(self):
        self.values(LARAVEL_ENV)
        ctx = self.f.ctx()
        link = "Login now works on https://admin.acme-corp.com/login, mail from no-reply@acme-corp.com"
        self.assertEqual(fx.scrub_text(ctx, link), (link, []))
        text, hits = fx.scrub_text(ctx, "The database password is Xy7kq2@ab1234")
        self.assertEqual(text, "The database password is [redacted by fx]")
        self.assertEqual(hits, ["local-env-value"])

    def test_names_and_values_that_stay_secret(self):
        for line, value in (("BASIC_AUTH=admin:Hunt3r2pw", "admin:Hunt3r2pw"), ("DB_PASSWORD_PROD=Hunt3r2pw", "Hunt3r2pw"),
                            ("PRIVATE_KEY_PEM=abcdefghij1", "abcdefghij1"), ("PGPASSWORD=Hunt3r2pw", "Hunt3r2pw"),
                            ("APIKEY=Hunt3r2pw", "Hunt3r2pw"), ("API_KEYS=Hunt3r2pw", "Hunt3r2pw"),
                            ("AWS_SECRET_ACCESS_KEY=Hunt3r2pw", "Hunt3r2pw"), ("SENTRY_DSN=Hunt3r2pw", "Hunt3r2pw"),
                            ("DB_PASSWORD=my.pass.word1", "my.pass.word1"), ("DB_PASSWORD=$Ecret1234", "$Ecret1234"),
                            ("JWT_SECRET=aaa111bbb.ccc222ddd.eee333fff", "aaa111bbb.ccc222ddd.eee333fff"),
                            ("API_TOKEN=abc123def.ghi456jkl", "abc123def.ghi456jkl"),
                            ("MAIL_PASSWORD=x.y@zmail.com", "x.y@zmail.com")):
            self.assertEqual(self.values(line + "\n"), {value}, line)

    def test_settings_and_references_are_not_secrets(self):
        for line in ("SESSION_DOMAIN=.acme-corp.com", "SESSION_LIFETIME=Abc1234", "COOKIE_DOMAIN=Abc1234", "AUTH_GUARD=Abc1234",
                     "CERT_PATH=Abc1234", "SIGNATURE_ALGO=Abc1234", "PRIVATE_BUCKET=Abc1234", "MONKEY=Hunt3r2pw", "BYPASS=Hunt3r2pw",
                     "VITE_API_HOST=app2.acme-123.com", "MAIL_FROM_ADDRESS=no-reply2@acme-123.com",
                     'VITE_PUSHER_APP_KEY="${PUSHER_APP_KEY}"', "DB_PASSWORD=${OTHER}", "APP_KEY=$OTHER_KEY"):
            self.assertEqual(self.values(line + "\n"), set(), line)


# --------------------------------------------------------------------------
# 8. the verifier must answer every blocking finding

class VerifierTest(FxTestCase):
    def setUp(self):
        super().setUp()
        self.f.save_job(stage="review", round=1, reviewers=["security"], reviews={"security": "pending"})
        self.rdir = os.path.join(self.f.jd, "round-1")
        write(os.path.join(self.rdir, "review-security.md"),
              "VERDICT: CHANGES\n\n## Findings\n1. [blocking] a.py:1: SQL injection\n"
              "2. [blocking] b.py:2: IDOR\n3. note only\n")

    def verify(self, text):
        write(os.path.join(self.rdir, "verify-security.md"), text)
        return self.fx("verdicts", "T-1")

    def test_bare_pass_is_rejected(self):
        code, res = self.verify("VERDICT: PASS\n")
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "verifier")

    def test_pass_with_a_confirmed_finding_is_rejected(self):
        code, res = self.verify("VERDICT: PASS\n\n## Findings\n1. rejected: a\n2. confirmed: b\n")
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "verifier")

    def test_every_finding_rejected_passes(self):
        code, res = self.verify("VERDICT: PASS\n\n## Findings\n1. rejected: a\n   Evidence: x\n"
                                "2. **Rejected**: b\n")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "approve")

    def test_changes_go_back_to_the_builder(self):
        code, res = self.verify("VERDICT: CHANGES\n\n## Findings\n1. confirmed: a\n2. unsure: b\n")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "build")

    def review(self, text):
        write(os.path.join(self.rdir, "review-security.md"), text)

    def test_unnumbered_findings_go_back_to_the_reviewer_first(self):
        for finding in ("- [blocking] a.py:1: SQL injection", "1) [blocking] x", "1. **[blocking]** x", "no format at all"):
            self.review("VERDICT: CHANGES\n\n## Findings\n%s\n" % finding)
            code, res = self.fx("verdicts", "T-1")
            self.assertEqual(code, 6, finding)
            self.assertEqual(res["retry_agent"], "security", finding)
            self.assertNotEqual(res.get("next"), "verify", finding)
            self.assertIn("no numbered", res["error"])

    def test_bare_pass_cannot_clear_unnumbered_findings(self):
        self.review("VERDICT: CHANGES\n\n## Findings\n- [blocking] a.py:1: SQL injection\n")
        code, res = self.verify("VERDICT: PASS\n")
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "security")
        self.assertEqual(self.f.job()["stage"], "review")

    def test_unnumbered_findings_go_back_even_without_a_verifier(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"verify_findings": False}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.review("VERDICT: CHANGES\n\n- [blocking] x\n")
        code, res = self.fx("verdicts", "T-1")
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "security")

    def test_numbered_findings_still_ask_for_the_verifier(self):
        self.review("VERDICT: CHANGES\n\n## Findings\n1. [blocking] [L12] a.py:1: SQL injection\n")
        code, res = self.fx("verdicts", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "verify")

    def test_pass_needs_no_numbered_finding(self):
        self.review("VERDICT: PASS\n\n## Checked\nAll fine.\n")
        code, res = self.fx("verdicts", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "approve")


# --------------------------------------------------------------------------
# 9. text posted to the PR and Jira is scanned

class OutgoingTextTest(FxTestCase):
    base_files = {"README.md": "project\n"}
    cfg = {"copy_into_worktree": [".env"], "jira": {"mode": "mcp"}, "security": {"required": []}}

    def setUp(self):
        super().setUp()
        write(os.path.join(self.f.factory, "local", ".env"), "DB_PASSWORD=Zx81-real-pw\n")

    def test_scrub_text(self):
        gh = "ghp_" + "aB3dE5" * 6
        text, hits = fx.scrub_text(self.f.ctx(), "token %s and pw Zx81-real-pw\nfine" % gh)
        self.assertNotIn(gh, text)
        self.assertNotIn("Zx81-real-pw", text)
        self.assertIn(fx.REDACTED, text)
        self.assertEqual(hits, ["github-token", "local-env-value"])

    def test_jira_comment_is_redacted(self):
        job = self.f.save_job(jira_base="https://example.atlassian.net")
        r = fx.jira_comment(self.f.ctx(), job, "the password is Zx81-real-pw", force=True)
        self.assertEqual(r["redacted"], ["local-env-value"])
        self.assertNotIn("Zx81-real-pw", job["jira_todo"][-1]["markdown"])

    def test_pr_body_is_redacted(self):
        key = "sk-ant-api03-" + "Zz9" * 10
        self.f.commit({"src/x.txt": "x\n"})
        write(os.path.join(self.f.jd, "decision.md"), "APPROVE\n\nThe builder used %s in a test.\n" % key)
        self.f.save_job(stage="pr", decision="APPROVE", summary="fix login (pw Zx81-real-pw)")
        code, res = self.fx("pr", "T-1")
        self.assertEqual(code, 0, res)
        body = slurp(os.path.join(self.f.jd, "pr-body.md"))
        self.assertNotIn(key, body)
        self.assertNotIn("Zx81-real-pw", body)
        self.assertIn(fx.REDACTED, body)
        self.assertTrue(any("redacted" in n for n in res["notes"]), res["notes"])


# --------------------------------------------------------------------------
# 10. pinned installs

class ToolPinTest(unittest.TestCase):
    def test_pipx_semgrep_is_pinned(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx, "install_method", return_value="pipx"), \
                mock.patch.object(fx.shutil, "which", return_value="/usr/bin/semgrep"):
            fx.install_one("semgrep")
        self.assertEqual(calls[0], ["pipx", "install", "semgrep==" + fx.SEMGREP_VERSION])

    def test_pins(self):
        tmp = tempfile.mkdtemp(prefix="fx-tools-")
        self.addCleanup(shutil.rmtree, tmp, True)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def which(name):
            return "/usr/bin/npm" if name == "npm" else None
        with mock.patch.dict(os.environ, {"FACTORY_TOOLS_DIR": tmp}), \
                mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx, "global_playwright_version", return_value=None), \
                mock.patch.object(fx, "playwright_ok", return_value=(True, "ok")), \
                mock.patch.object(fx.shutil, "which", side_effect=which), \
                contextlib.redirect_stdout(io.StringIO()):
            fx.cmd_tools(None, argparse.Namespace(action="install", names=["playwright", "semgrep"]))
        npm = [c for c in calls if c[0] == "npm"][0]
        self.assertIn("playwright@" + fx.PLAYWRIGHT_VERSION, npm)
        self.assertIn("axe-core@" + fx.AXE_CORE_VERSION, npm)
        pip = [c for c in calls if "pip" in c][0]
        self.assertIn("semgrep==" + fx.SEMGREP_VERSION, pip)
        for v in (fx.PLAYWRIGHT_VERSION, fx.AXE_CORE_VERSION, fx.SEMGREP_VERSION):
            self.assertRegex(v, r"^\d+\.\d+\.\d+$")


# --------------------------------------------------------------------------
# the script itself

class ScriptTest(unittest.TestCase):
    def test_help_and_compile(self):
        p = subprocess.run([sys.executable, "-I", FX_PATH, "--help"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("gate", p.stdout)
        out = tempfile.mkdtemp(prefix="fx-compile-")
        self.addCleanup(shutil.rmtree, out, True)
        p = subprocess.run([sys.executable, "-I", "-c", "import py_compile,sys; py_compile.compile(sys.argv[1], "
                            "cfile=sys.argv[2], doraise=True)", FX_PATH, os.path.join(out, "fx.pyc")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_versions_agree(self):
        with open(os.path.join(SKILL, "VERSION")) as f:
            v = f.read().strip()
        with open(os.path.join(ROOT, "plugins", "code-factory", ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["version"], v)
        self.assertEqual(v, "1.0.0")


if __name__ == "__main__":
    unittest.main()
