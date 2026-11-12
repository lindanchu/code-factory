"""Tests for the gate of a project of several repositories (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

The gate checks each changed repository in repo_order and stops at the first that fails; the apps start in dependency
order, each on its own port; the scanners, selftest and audit-scan work per repository. Every origin is a real bare
repository, every app a small Python HTTP server.
"""

import json
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

from test_project_repos import ProjectCase
from test_security import fx, load_json, sh, slurp, write

APP = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
with open(sys.argv[2], "w") as f:
    json.dump(dict(os.environ, CWD=os.getcwd()), f)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''

FINDING = json.dumps({"errors": [], "results": [{"check_id": "sqli", "path": "pay.py", "start": {"line": 1},
                                                 "extra": {"severity": "ERROR", "message": "sql built from input",
                                                           "metadata": {}}}]})


def free_base(span=40):
    """A ports.base with span free ports after it (apps of these tests listen there)."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        if base + span > 65000:
            continue
        ok = True
        for p in range(base, base + span):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    raise unittest.SkipTest("no free ports")


class GateCase(ProjectCase):
    def to_build(self, job, **fields):
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j.update(stage="build", risk=j.get("risk") or "high", **fields)
        fx.route(ctx, j)
        ctx.save(j)

    job = None

    def job_for(self, repos="be,fe"):
        job = self.new("--repos", repos)["job"]
        self.to_build(job)
        self.job = job
        return job

    def gate(self, job=None):
        return self.fx("gate", job or self.job)

    def checks_md(self, job=None, n=1):
        return slurp(os.path.join(self.factory, "jobs", job or self.job, "round-%d" % n, "checks.md"))

    def round_file(self, name, job=None, n=1):
        return os.path.join(self.factory, "jobs", job or self.job, "round-%d" % n, name)

    def push_base(self, name, files):
        """Add files to a repository's main on its origin, before a job starts."""
        tmp = os.path.join(self.tmp, "seed-add-" + name)
        sh(["git", "clone", "-q", self.origin[name], tmp], self.tmp)
        for rel, text in files.items():
            write(os.path.join(tmp, rel), text)
        sh(["git", "add", "-A"], tmp)
        sh(["git", "commit", "-q", "-m", "more"], tmp)
        sh(["git", "push", "-q", "origin", "HEAD:main"], tmp)


# --------------------------------------------------------------------------
# checks per repository, the first failure stops the gate

class TwoRepoGateTest(GateCase):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.factory, "checks", "fe.sh"), "echo fe checks ran\nexit 3\n")
        self.commit_factory()
        self.job = self.job_for()
        for n in ("be", "fe"):
            self.commit_in(self.work(self.job, n), "%s.txt" % n, "work\n")

    def test_a_failure_in_the_second_repository_is_named(self):
        code, res = self.gate()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["gate"], "FAIL")
        self.assertEqual(res["repos"], {"be": "pass", "fe": "fail"})
        md = self.checks_md()
        self.assertIn("- fe: checks/fe.sh exited with 3", md)
        self.assertIn("## Repo: be", md)
        self.assertIn("## Repo: fe", md)
        self.assertIn("### checks/fe.sh output (last 150 lines, full log in checks-fe.log)", md)
        self.assertIn("fe checks ran", slurp(self.round_file("checks-fe.log")))
        self.assertTrue(os.path.exists(self.round_file("checks-be.log")))

    def test_the_first_failure_stops_the_gate(self):
        write(os.path.join(self.factory, "checks", "be.sh"), "exit 1\n")
        self.commit_factory()
        code, res = self.gate()
        self.assertEqual(res["repos"], {"be": "fail", "fe": "not checked"})
        md = self.checks_md()
        self.assertIn("- be: checks/be.sh exited with 1", md)
        self.assertIn("Not checked: the gate stopped at be.", md)
        self.assertFalse(os.path.exists(self.round_file("checks-fe.log")))

    def test_a_repository_without_commits_is_not_changed(self):
        sh(["git", "reset", "-q", "--hard", "HEAD~1"], self.work(self.job, "fe"))  # fe needed nothing after all
        code, res = self.gate()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(res["repos"], {"be": "pass", "fe": "not changed"})
        md = self.checks_md()
        self.assertIn("## Repo: fe", md)
        self.assertIn("Not changed: no commits beyond origin/main, so its checks and scans are skipped", md)
        self.assertNotIn("fe checks ran", md)  # checks/fe.sh (exit 3) never ran
        self.assertFalse(os.path.exists(self.round_file("checks-fe.log")))
        self.assertFalse(os.path.exists(self.round_file("security-fe")))
        self.assertIn("## Repo: be", slurp(self.round_file("security.md")))
        self.assertNotIn("## Repo: fe", slurp(self.round_file("security.md")))

    def test_no_commits_anywhere_still_fails(self):
        for n in ("be", "fe"):
            sh(["git", "reset", "-q", "--hard", "HEAD~1"], self.work(self.job, n))
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("- be: The branch has no commits beyond origin/main.", self.checks_md())

    def test_uncommitted_work_in_a_repository_without_commits_still_fails(self):
        wt = self.work(self.job, "fe")
        sh(["git", "reset", "-q", "--hard", "HEAD~1"], wt)
        write(os.path.join(wt, "README.md"), "half done\n")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertEqual(res["repos"]["fe"], "fail")
        self.assertIn("Uncommitted changes", self.checks_md())

    def test_a_missing_checks_script_is_the_factory_s(self):
        sh(["git", "rm", "-q", "checks/fe.sh"], self.factory)
        self.commit_factory()
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn("checks/fe.sh", res["error"])
        self.assertEqual(self.raw(self.job)["round"], 0)


class ProtectedPerRepoTest(GateCase):
    extra = {"fe": {"protected_paths": ["tests/**"]}}

    def test_each_repository_s_own_protected_paths(self):
        for n in ("be", "fe"):
            self.push_base(n, {"tests/a.txt": "old\n"})
        job = self.job_for()
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "tests/a.txt", "changed\n")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md()
        self.assertIn("- fe: Protected file modified: tests/a.txt", md)
        self.assertNotIn("be: Protected", md)
        self.assertEqual(res["repos"], {"be": "pass", "fe": "fail"})


class ChecksTamperTest(GateCase):
    def setUp(self):
        super().setUp()
        self.job = self.job_for()
        for n in ("be", "fe"):
            self.commit_in(self.work(self.job, n), "%s.txt" % n, "work\n")

    def test_an_uncommitted_change_to_another_repository_s_script(self):
        write(os.path.join(self.factory, "checks", "fe.sh"), "exit 0 # tampered\n")
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn("checks/fe.sh", res["error"])

    def test_a_helper_file_a_script_sources(self):
        write(os.path.join(self.factory, "checks", "_lib.sh"), "ok() { echo helper ran; }\n")
        write(os.path.join(self.factory, "checks", "be.sh"), '. "$(dirname "$0")/_lib.sh"\nok\n')
        self.commit_factory()
        write(os.path.join(self.factory, "checks", "_lib.sh"), "ok() { echo tampered; }\n")
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn("checks/_lib.sh", res["error"])
        sh(["git", "checkout", "--", "checks/_lib.sh"], self.factory)
        write(os.path.join(self.factory, "checks", "_more.sh"), "ok() { :; }\n")
        code, res = self.fx("gate", self.job)
        self.assertEqual(code, 3, res)
        self.assertIn("not committed: checks/_more.sh", res["error"])
        os.remove(os.path.join(self.factory, "checks", "_more.sh"))
        # the project's code, run by a checks script, rewrites the helper while the gate runs
        lib = os.path.join(self.factory, "checks", "_lib.sh")
        write(os.path.join(self.factory, "checks", "be.sh"), '. "$(dirname "$0")/_lib.sh"\necho "# x" >> %s\nok\n' % lib)
        self.commit_factory()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("the gate's settings changed while it ran", self.checks_md())
        self.assertIn("checks/_lib.sh", self.checks_md())

    def test_one_repository_s_checks_changing_another_s_script(self):
        target = os.path.join(self.factory, "checks", "fe.sh")
        write(os.path.join(self.factory, "checks", "be.sh"), "echo '# tampered' >> %s\nexit 0\n" % target)
        self.commit_factory()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md()
        self.assertIn("the gate's settings changed while it ran", md)
        self.assertIn("checks/fe.sh", md)


# --------------------------------------------------------------------------
# apps in dependency order, each on its port, with the URLs of the ones before it

class AppsCase(GateCase):
    def setUp(self):
        self.base = free_base()
        self.server = None
        super().setUp()

    def config(self):
        cfg = super().config()
        self.server = os.path.join(self.tmp, "server.py")
        write(self.server, APP)
        self.order = os.path.join(self.tmp, "order.txt")
        self.out_dir = os.path.join(self.tmp, "app-out")
        os.makedirs(self.out_dir, exist_ok=True)
        cfg["ports"] = {"base": self.base, "step": 20}
        cfg["security"] = {"scanners": {"zap": False}}
        for r in cfg["repos"]:
            n = r["name"]
            r["app"] = {"up": "echo %s >> %s && exec %s %s {port} %s" % (
                n, self.order, sys.executable, self.server, os.path.join(self.out_dir, "env-%s.json" % n)),
                "down": "echo down-%s >> %s" % (n, self.order), "start_timeout_sec": 20}
        cfg["repos"][1]["app"].update(needs=["be"], env={"API_URL": "{url:be}/api", "OWN": "{port}"})
        return cfg

    def tearDown(self):
        subprocess.run(["pkill", "-f", self.server], check=False)
        super().tearDown()

    def env_of(self, n):
        return load_json(os.path.join(self.out_dir, "env-%s.json" % n))


class AppOrderTest(AppsCase):
    def test_a_web_change_runs_the_backend_it_needs_first(self):
        job = self.job_for("fe")
        raw = self.raw(job)
        self.assertEqual(raw["repos"]["be"]["role"], "run")
        self.commit_in(self.work(job, "fe"), "page.txt", "page\n")
        self.addCleanup(self.fx, "app", "down", job)
        code, res = self.gate(job)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["gate"], "PASS", self.checks_md(job))
        block = raw["port"]
        be_port, fe_port = block, block + 1
        self.assertEqual(slurp(self.order).split(), ["down-fe", "down-be", "be", "fe"])
        be, fe = self.env_of("be"), self.env_of("fe")
        self.assertEqual(be["PORT"], str(be_port))
        self.assertEqual(fe["PORT"], str(fe_port))
        self.assertEqual(fe["FACTORY_URL_BE"], "http://127.0.0.1:%d" % be_port)
        self.assertEqual(fe["API_URL"], "http://127.0.0.1:%d/api" % be_port)
        self.assertEqual(fe["OWN"], str(fe_port))
        self.assertNotIn("FACTORY_URL_FE", be)
        # every app knows its own URL and every one started before it, by name and as one JSON object
        self.assertEqual(fe["FACTORY_URL_FE"], "http://127.0.0.1:%d" % fe_port)
        self.assertEqual(be["FACTORY_URL_BE"], "http://127.0.0.1:%d" % be_port)
        self.assertEqual(json.loads(fe["FACTORY_APP_URLS"]), {"be": "http://127.0.0.1:%d" % be_port,
                                                             "fe": "http://127.0.0.1:%d" % fe_port})
        self.assertEqual(json.loads(be["FACTORY_APP_URLS"]), {"be": "http://127.0.0.1:%d" % be_port})
        self.assertNotEqual(be["FACTORY_DB"], fe["FACTORY_DB"])
        self.assertTrue(fe["FACTORY_DB"].endswith("_fe"), fe["FACTORY_DB"])
        self.assertNotEqual(be["COMPOSE_PROJECT_NAME"], fe["COMPOSE_PROJECT_NAME"])
        # the backend runs from its base branch, in its run-only checkout
        self.assertEqual(os.path.realpath(be["CWD"]), os.path.realpath(self.work(job, "be")))
        self.assertEqual(os.path.realpath(fe["CWD"]), os.path.realpath(self.work(job, "fe")))
        jd = os.path.join(self.factory, "jobs", job)
        self.assertTrue(os.path.exists(os.path.join(jd, "app-be.json")))
        self.assertTrue(os.path.exists(os.path.join(jd, "app-fe.json")))
        status = self.ok("app", "status", job)
        self.assertEqual(sorted(status["apps"]), ["be", "fe"])
        self.assertTrue(all(a["healthy"] for a in status["apps"].values()))
        # headers are looked at on the app of the repository the job changed only
        sec = slurp(self.round_file("security.md", job))
        self.assertIn("## Repo: fe", sec)
        self.assertIn(":%d" % fe_port, sec)
        self.assertNotIn(":%d/" % be_port, sec)
        # stopped in the reverse order
        self.ok("app", "down", job)
        self.assertEqual(slurp(self.order).split()[-2:], ["down-fe", "down-be"])
        self.assertFalse(os.path.exists(os.path.join(jd, "app-be.json")))

    def test_an_app_that_does_not_start_stops_the_ones_before_it(self):
        cfg = self.config()
        cfg["repos"][1]["app"]["up"] = "exit 4"
        self.write_config(cfg)
        self.commit_factory()
        job = self.job_for("fe")
        self.commit_in(self.work(job, "fe"), "page.txt", "page\n")
        code, res = self.gate(job)
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md(job)
        self.assertIn("the apps could not start: the app of fe did not come up", md)
        self.assertFalse(os.path.exists(os.path.join(self.factory, "jobs", job, "app-be.json")))


class PortsTest(GateCase):
    def test_apps_agents_and_candidates_never_share_a_port(self):
        for step in (None, 10, 30):
            cfg = self.config()
            if step:
                cfg["ports"] = {"base": 5100, "step": step}
            else:
                cfg.pop("ports")
            self.write_config(cfg)
            ctx = self.ctx()
            for slot in (1, 2):
                job = {"id": "X", "slot": slot, "port": fx.ports_base(ctx) + fx.ports_step(ctx) * slot,
                       "repo_order": ["be", "fe"], "repos": {"be": {}, "fe": {}}}
                ports = [fx.job_port(ctx, job, n) for n in ("be", "fe")]
                ports += [fx.agent_port(ctx, job, off) for off in (0, 1, 2)]
                for k in range(1, fx.candidates_room(ctx) + 1):
                    ports += [fx.candidate_block(ctx, job, k) + i for i in (0, 1)]
                self.assertEqual(len(ports), len(set(ports)), (step, ports))
                lo = job["port"]
                self.assertTrue(all(lo <= p < lo + fx.ports_step(ctx) for p in ports), (step, ports))
            self.assertGreaterEqual(fx.candidates_room(ctx), 2, step)
        self.assertGreaterEqual(fx.ports_step(ctx), 13)  # no step set: room for 2 apps, 3 agents, 4 candidates

    def test_a_builder_s_dev_port_is_not_an_app_s(self):
        job = self.new("--repos", "be,fe")["job"]
        raw = self.raw(job)
        self.to_build(job)
        res = self.ok("ctx", job, "--agent", "builder")
        port = int([l for l in res["prompt"].splitlines() if l.startswith("Port: ")][0].split()[1])
        self.assertNotIn(port, [raw["port"], raw["port"] + 1])

    def test_best_of_n_never_outgrows_the_block(self):
        cfg = self.config()
        cfg["ports"] = {"base": 5100, "step": 9}  # 2 apps + 3 agents leave room for 2 candidates
        self.write_config(cfg)
        self.commit_factory()
        res = self.new("--repos", "be,fe", "--best-of", "4")
        self.assertEqual(self.raw(res["job"])["best_of"], 2)
        self.assertIn("best-of-4 reduced to 2", res["notes"][0])
        cfg["ports"]["step"] = 6  # room for no candidate at all
        self.write_config(cfg)
        self.commit_factory()
        write(self.issue, "# PROJ-8: other\n")
        res = self.new("--repos", "be,fe", "--best-of", "3")
        self.assertNotIn("best_of", self.raw(res["job"]))
        self.assertIn("best-of-3 dropped, one builder instead", res["notes"][0])

    def test_a_new_job_never_takes_ports_an_older_job_holds(self):
        first = self.new("--repos", "be,fe")["job"]
        old = self.raw(first)
        self.assertEqual((old["slot"], old["port"], old["port_span"]), (1, 5110, 10))
        cfg = self.config()
        cfg["ports"] = {"base": 5100, "step": 7}  # slot 1 would now be 5107-5113: inside the older job's 5110-5119
        self.write_config(cfg)
        self.commit_factory()
        write(self.issue, "# PROJ-8: other\n")
        second = self.raw(self.new("--repos", "be,fe")["job"])
        lo, hi = second["port"], second["port"] + 7
        self.assertTrue(hi <= 5110 or lo >= 5120, (lo, hi))
        self.assertNotEqual(second["slot"], 1)


class RunCloneGateTest(AppsCase):
    """fe's app needs be's; a job that changes fe only runs be from a checkout of its base."""

    def test_work_in_a_run_only_clone_fails_the_gate(self):
        job = self.job_for("fe")
        self.addCleanup(self.fx, "app", "down", job)
        self.commit_in(self.work(job, "fe"), "page.txt", "page\n")
        write(os.path.join(self.work(job, "be"), "README.md"), "changed by mistake\n")
        code, res = self.gate(job)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md(job)
        self.assertIn("be is run-only: run `fx repos %s --add be` to change it" % job, md)
        sh(["git", "checkout", "--", "README.md"], self.work(job, "be"))
        self.commit_in(self.work(job, "be"), "fix.py", "x\n")
        code, res = self.gate(job)
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("be is run-only: run `fx repos %s --add be` to change it (1 commit(s))" % job,
                      self.checks_md(job, 2))


# --------------------------------------------------------------------------
# security per repository

class SecurityPerRepoTest(GateCase):
    extra = {"be": {"security": {"accepted_rules": [{"rule": "sqli", "reason": "reviewed: prepared upstream"}],
                                 "semgrep_exclude": ["legacy"]}}}

    def test_each_repository_has_its_own_accepted_rules_and_excludes(self):
        job = self.job_for()
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "pay.py", "q = 1\n")
        with mock.patch.dict(os.environ, STUB_SEMGREP_OUT=FINDING):
            code, res = self.gate(job)
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("security scan: 1 blocking finding(s)", self.checks_md(job))
        sec = slurp(self.round_file("security.md", job))
        be_part, fe_part = sec.split("## Repo: fe")
        self.assertIn("## Repo: be", be_part)
        self.assertIn("accepted: reviewed: prepared upstream", be_part)
        self.assertIn("| yes | high |", fe_part)
        self.assertTrue(os.path.exists(self.round_file("security-be/semgrep.json", job)))
        calls = [json.loads(l) for l in open(os.path.join(self.tmp, "stub.log")) if '"semgrep"' in l]
        argv = {os.path.basename(c["cwd"]): c["argv"] for c in calls if c["tool"] == "semgrep" and "scan" in c["argv"]}
        excludes = {n: [a[i + 1] for i, x in enumerate(a) if x == "--exclude"] for n, a in argv.items()}
        self.assertEqual(excludes["be"], ["legacy"])
        self.assertIn("node_modules", excludes["fe"])

    def test_fx_security_scans_every_changed_repository(self):
        job = self.job_for()
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "pay.py", "q = 1\n")
        with mock.patch.dict(os.environ, STUB_SEMGREP_OUT=FINDING):
            res = self.ok("security", job)
        self.assertEqual(res["blocking"], 1)
        self.assertEqual(res["repos"]["be"]["blocking"], 0)
        self.assertEqual(res["repos"]["fe"]["blocking"], 1)


class BaselineShapesTest(GateCase):
    def ctx_with(self, data):
        write(os.path.join(self.factory, "baseline.json"), json.dumps(data))
        return self.ctx()

    def test_the_new_shape_is_per_repository(self):
        ctx = self.ctx()
        sig = fx.scan_signature(ctx, "headers")
        ctx = self.ctx_with({"repos": {"be": {"security": {"headers": []}, "security_scan": {"headers": sig}}}})
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, "be"), "headers")[0], "ok")
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, "fe"), "headers")[0], "none")

    def test_an_old_flat_one_is_the_first_repository_s(self):
        ctx = self.ctx()
        sig = fx.scan_signature(ctx, "headers")
        ctx = self.ctx_with({"security": {"headers": []}, "security_scan": {"headers": sig}})
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, "be"), "headers")[0], "ok")
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, "fe"), "headers")[0], "none")
        self.assertEqual(fx.baseline_state(ctx, "headers")[0], "ok")

    def test_an_old_factory_reads_its_flat_one(self):
        self.write_config({"project": {"name": "shop", "url": self.origin["be"]}, "base_branch": "main"})
        ctx = self.ctx()
        sig = fx.scan_signature(ctx, "headers")
        ctx = self.ctx_with({"security": {"headers": []}, "security_scan": {"headers": sig}})
        self.assertEqual(fx.baseline_state(ctx, "headers")[0], "ok")
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, ctx.repo_names()[0]), "headers")[0], "ok")


# --------------------------------------------------------------------------
# selftest and audit-scan per repository

class SelftestTest(AppsCase):
    def config(self):
        cfg = super().config()
        cfg["repos"][0]["app"]["schema"] = "echo 'CREATE TABLE be_users (id int);'"
        cfg["repos"][1]["e2e"] = 'env > %s' % os.path.join(self.tmp, "fe-e2e.env")
        return cfg

    def test_every_repository_and_one(self):
        res = self.ok("selftest", "--no-zap")
        self.assertTrue(res["ok"], json.dumps(res, indent=1)[:3000])
        self.assertEqual(sorted(res["repos"]), ["be", "fe"])
        b = load_json(os.path.join(self.factory, "baseline.json"))
        self.assertEqual(sorted(b["repos"]), ["be", "fe"])
        fe = b["repos"]["fe"]
        self.assertTrue(fe["steps"]["checks"]["ok"])
        self.assertTrue(fe["steps"]["app"]["ok"])
        self.assertIn("be", fe["steps"]["app"]["needs"])
        self.assertIn("headers", fe["security"])
        self.assertIn("headers", fe["security_scan"])
        self.assertIn("CREATE TABLE be_users", slurp(os.path.join(self.factory, "jobs", "_selftest", "schema-be.txt")))
        e2e = dict(l.split("=", 1) for l in slurp(os.path.join(self.tmp, "fe-e2e.env")).splitlines() if "=" in l)
        self.assertEqual(e2e["BASE_URL"], fe["steps"]["app"]["url"].rstrip("/"))
        self.assertEqual(e2e["FACTORY_URL_BE"], fe["steps"]["app"]["needs"]["be"].rstrip("/"))
        self.assertFalse(os.path.exists(self.work("_selftest-be")))
        self.assertFalse(os.path.exists(self.work("_selftest-fe")))
        # the selftest's own baseline counts for each repository's scans
        ctx = self.ctx()
        self.assertEqual(fx.baseline_state(fx.repo_ctx(ctx, "fe"), "headers")[0], "ok")
        # one repository: the other keeps what it had
        be_at = b["repos"]["be"]["at"]
        res = self.ok("selftest", "--repo", "fe", "--no-zap", "--no-app")
        self.assertEqual(res["repo"], "fe")
        b2 = load_json(os.path.join(self.factory, "baseline.json"))
        self.assertEqual(b2["repos"]["be"]["at"], be_at)
        self.assertNotIn("app", b2["repos"]["fe"]["steps"])


class AuditScanTest(GateCase):
    def test_findings_and_task_files_carry_the_repository(self):
        with mock.patch.dict(os.environ, STUB_SEMGREP_OUT=FINDING):
            res = self.ok("audit-scan", "--no-app")
        self.assertEqual(res["repos"], ["be", "fe"])
        self.assertEqual(res["branches"], {"be": "main", "fe": "main"})
        self.assertEqual(sorted(r["repo"] for r in res["to_fix"]), ["be", "fe"])
        for r in res["to_fix"]:
            self.assertIn("-%s-" % r["repo"], os.path.basename(r["task_file"]))
            self.assertIn("Repository: %s" % r["repo"], slurp(r["task_file"]))
        report = slurp(res["report"])
        self.assertIn("## Repo: be", report)
        self.assertIn("## Repo: fe", report)
        with mock.patch.dict(os.environ, STUB_SEMGREP_OUT=FINDING):
            res = self.ok("audit-scan", "--no-app", "--repo", "fe", "--base", "fe=develop")
        self.assertEqual(res["repo"], "fe")
        self.assertEqual(res["branch"], "develop")
        self.assertEqual([r["repo"] for r in res["to_fix"]], ["fe"])
        self.assertNotIn("## Repo:", slurp(res["report"]))


# --------------------------------------------------------------------------
# baselines kept in one repository of the project

class BaselinesRepoTest(GateCase):
    store = {"bucket": "shots", "region": "eu-west-1"}

    def cfg_with(self, repo):
        cfg = self.config()
        cfg["storage"] = self.store
        cfg["baselines"] = {"dir": "shots", "manifest": "shots.json", "repo": repo}
        self.write_config(cfg)
        return self.ctx()

    def test_naming_a_repository(self):
        ctx = self.cfg_with("fe")
        bl = fx.baselines_config(ctx)
        self.assertEqual((bl["repo"], bl["repo_name"]), ("project", "fe"))
        job = self.new("--repos", "be,fe")["job"]
        whole = ctx.load_exact(job)
        self.assertEqual(fx.baselines_job(ctx, bl, whole)["worktree"], self.work(job, "fe"))
        self.assertEqual(fx.baselines_root(ctx, bl, job=whole), self.work(job, "fe"))
        self.assertEqual(fx.baselines_env(fx.repo_ctx(ctx, "be"), self.work(job, "be")), {})
        self.assertEqual(fx.baselines_env(fx.repo_ctx(ctx, "fe"), self.work(job, "fe")),
                         {"FACTORY_BASELINES": os.path.join(self.work(job, "fe"), "shots")})
        self.assertIsNone(fx.baselines_fill_clone(ctx, self.work(job, "be"), "be"))

    def test_a_job_that_does_not_change_it(self):
        ctx = self.cfg_with("fe")
        bl = fx.baselines_config(ctx)
        job = self.new("--repos", "be")["job"]
        whole = ctx.load_exact(job)
        self.assertIsNone(fx.baselines_job(ctx, bl, whole))
        self.assertEqual(fx.baselines_for_checks(ctx, bl, whole), (True, ""))
        self.assertEqual(fx.baselines_before_push(ctx, bl, whole), [])
        with self.assertRaises(fx.FxError) as e:
            fx.baselines_root(ctx, bl, job=whole)
        self.assertIn("fe", str(e.exception))

    def test_project_needs_a_name_with_several(self):
        ctx = self.cfg_with("project")
        with self.assertRaises(fx.FxError) as e:
            fx.baselines_config(ctx)
        self.assertIn("name the one that holds the list (be, fe)", str(e.exception))
        ctx = self.cfg_with("web")
        with self.assertRaises(fx.FxError) as e:
            fx.baselines_config(ctx)
        self.assertIn("one of the repositories (be, fe)", str(e.exception))

    def test_project_still_means_the_only_repository(self):
        cfg = self.config()
        cfg["repos"] = cfg["repos"][:1]
        cfg["storage"] = self.store
        cfg["baselines"] = {"dir": "shots", "manifest": "shots.json", "repo": "project"}
        self.write_config(cfg)
        bl = fx.baselines_config(self.ctx())
        self.assertEqual((bl["repo"], bl["repo_name"]), ("project", "be"))


# --------------------------------------------------------------------------
# split builds: findings go to the package of their repository and file

class WpFeedbackTest(GateCase):
    def test_routed_by_repository_and_path(self):
        job = self.new("--repos", "be,fe")["job"]
        self.to_build(job)
        plan = {"packages": [{"id": "wp0", "title": "api", "repo": "be", "owns": ["api/**"]},
                             {"id": "wp1", "title": "form", "repo": "fe", "owns": ["api/**"]}]}
        write(os.path.join(self.factory, "jobs", job, "plan.md"), "PLAN: SPLIT\n\n```json\n%s\n```\n" % json.dumps(plan))
        self.ok("split", job)
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["round"] = 1
        ctx.save(j)
        rdir = os.path.join(self.factory, "jobs", job, "round-1")
        write(os.path.join(rdir, "checks.md"), "GATE: FAIL\n\n## Problems\n\n- be: api/lint.js fails the linter\n\n")
        write(os.path.join(rdir, "review-code.md"), "VERDICT: CHANGES\n\n"
                                                    "1. [blocking] fe:api/form.js:3 the form posts twice\n"
                                                    "2. [blocking] `be:api/users.js` returns the hash\n"
                                                    "3. [blocking] api/shared.js is in both\n")
        res = self.ok("wp", "feedback", job)
        self.assertEqual(res["packages"], ["wp0", "wp1"])
        self.assertTrue(res["integrator"])
        wp0 = slurp(os.path.join(rdir, "feedback-wp0.md"))
        wp1 = slurp(os.path.join(rdir, "feedback-wp1.md"))
        self.assertIn("users.js", wp0)
        self.assertIn("lint.js", wp0)
        self.assertNotIn("form.js", wp0)
        self.assertIn("form.js", wp1)
        self.assertNotIn("users.js", wp1)
        self.assertIn("shared.js", slurp(os.path.join(rdir, "feedback-integrator.md")))

    def test_places_in_a_line(self):
        self.assertEqual(fx.finding_places("[gate] be: checks failed in api/x.js:4", ["be", "fe"]), [("be", "api/x.js")])
        self.assertEqual(fx.finding_places("[code] 1. [blocking] fe:src/a.ts and src/b.ts", ["be", "fe"]),
                         [("fe", "src/a.ts"), ("", "src/b.ts")])  # a plain path no clone was asked about: no package's
        self.assertEqual(fx.finding_places("[code] 1. [blocking] src/b.ts", ["main"]), [(None, "src/b.ts")])

    def test_a_plain_path_goes_to_the_only_repository_that_has_the_file(self):
        roots = {n: os.path.join(self.tmp, "roots", n) for n in ("be", "fe")}
        write(os.path.join(roots["be"], "src", "only-be.ts"), "x\n")
        for n in ("be", "fe"):
            write(os.path.join(roots[n], "src", "both.ts"), "x\n")
        line = "[code] 1. [blocking] src/only-be.ts:3, src/both.ts and src/none.ts"
        self.assertEqual(fx.finding_places(line, ["be", "fe"], roots),
                         [("be", "src/only-be.ts"), ("", "src/both.ts"), ("", "src/none.ts")])

    def test_a_plain_path_in_both_repositories_goes_to_the_integrator(self):
        job = self.new("--repos", "be,fe")["job"]
        self.to_build(job)
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "api/common.js", "x\n")
        self.commit_in(self.work(job, "fe"), "api/form.js", "x\n")
        plan = {"packages": [{"id": "wp0", "title": "api", "repo": "be", "owns": ["api/**"]},
                             {"id": "wp1", "title": "form", "repo": "fe", "owns": ["api/**"]}]}
        write(os.path.join(self.factory, "jobs", job, "plan.md"), "PLAN: SPLIT\n\n```json\n%s\n```\n" % json.dumps(plan))
        self.ok("split", job)
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["round"] = 1
        ctx.save(j)
        rdir = os.path.join(self.factory, "jobs", job, "round-1")
        write(os.path.join(rdir, "review-code.md"), "VERDICT: CHANGES\n\n1. [blocking] api/common.js:1 leaks\n"
                                                    "2. [blocking] api/form.js:1 posts twice\n")
        res = self.ok("wp", "feedback", job)
        self.assertEqual(res["packages"], ["wp1"])
        self.assertIn("form.js", slurp(os.path.join(rdir, "feedback-wp1.md")))
        self.assertIn("common.js", slurp(os.path.join(rdir, "feedback-integrator.md")))
        self.assertNotIn("common.js", slurp(os.path.join(rdir, "feedback-wp1.md")))


# --------------------------------------------------------------------------
# the fit checker reviews a job of two or more changed repositories

class FitReviewerTest(GateCase):
    def test_routed_when_two_repositories_change(self):
        two = self.raw(self.job_for("be,fe"))
        self.assertIn("fit", two["reviewers"])
        self.assertIn("fit", two["models"])
        one = self.raw(self.job_for("be"))
        self.assertNotIn("fit", one["reviewers"])

    def test_the_gate_names_it_and_verdicts_read_it(self):
        job = self.job_for("be,fe")
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "%s.txt" % n, "work\n")
        code, res = self.gate(job)
        self.assertEqual(res["gate"], "PASS", self.checks_md(job))
        self.assertIn("fit", res["reviewers"])
        self.assertEqual(res["agents"]["fit"], "factory-fit-checker")
        self.assertEqual(res["agents"]["code"], "factory-reviewer-code")
        ctx_ = self.ok("ctx", job, "--agent", "fit")
        self.assertIn(os.path.join("round-1", "review-fit.md"), ctx_["prompt"])
        self.assertEqual(ctx_["model"], self.raw(job)["models"]["fit"])
        rdir = os.path.join(self.factory, "jobs", job, "round-1")
        for r in res["reviewers"]:
            if r != "fit":
                write(os.path.join(rdir, "review-%s.md" % r), "VERDICT: PASS\n")
        res = self.ok("verdicts", job)
        self.assertEqual((res["next"], res["missing"]), ("wait", ["fit"]))
        write(os.path.join(rdir, "review-fit.md"), "VERDICT: CHANGES\n\n1. [blocking] be:api.py and fe:form.js disagree\n")
        res = self.ok("verdicts", job)
        self.assertEqual((res["next"], res["changes"]), ("build", ["fit"]))

    def test_a_bad_first_line(self):
        job = self.job_for("be,fe")
        for n in ("be", "fe"):
            self.commit_in(self.work(job, n), "%s.txt" % n, "work\n")
        res = self.gate(job)[1]
        rdir = os.path.join(self.factory, "jobs", job, "round-1")
        for r in res["reviewers"]:
            write(os.path.join(rdir, "review-%s.md" % r), "VERDICT: PASS\n" if r != "fit" else "FIT PASS\n")
        code, res = self.fx("verdicts", job)
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "fit")


if __name__ == "__main__":
    unittest.main()
