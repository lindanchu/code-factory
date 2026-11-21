#!/usr/bin/env python3
"""A colleague's merge request runs in its review's box, in the sandbox VM, never on this machine: its setup, app,
checks, commands and browser get only what fx passes them, the box loses its way out once installed, is deleted at the
end, and a machine without the sandbox stops the review before anything of the merge request runs.

The first tests use the limactl stub (test_security.STUB): what the box runs runs here, with only the variables fx
gave the box, and every command is logged. The last ones (RealSandboxTest) need the real VM: FX_SANDBOX_REAL=1 with
`fx sandbox install` done; they run the review's proof of concept against it."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from test_review_sandbox import SandboxCase
from test_security import FxTestCase, fx, load_json, slurp, write


class InTheBoxTest(SandboxCase):
    def setUp(self):
        super().setUp()
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["setup"] = "echo setup-ran"
        cfg["review"] = {"checks": ["echo checked"]}
        write(path, json.dumps(cfg))

    def box_cmds(self):
        return [c for c in self.f.calls("box")]

    def vm_scripts(self):
        return [c["argv"] for c in self.f.calls("limactl") if c["argv"][:1] == ["shell"]]

    def test_setup_app_and_checks_run_in_the_box(self):
        rid, res = self.up()
        cmds = [c["cmd"] for c in self.box_cmds()]
        self.assertIn("echo setup-ran", cmds)
        self.assertIn("echo checked", cmds)
        self.assertTrue(any("setsid bash -lc" in c and self.server in c for c in cmds), cmds)
        job = self.job()
        self.assertTrue(job["sandbox"]["box"]["name"].startswith("fxbox-"))

    def test_the_box_gets_only_what_fx_passed(self):
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": "FAKE_JIRA", "AWS_SECRET_ACCESS_KEY": "FAKE_AWS"}):
            self.up()
        for c in self.box_cmds():
            self.assertFalse({"PATH", "HOME", "USER", "JIRA_API_TOKEN", "AWS_SECRET_ACCESS_KEY"} & set(c["env"]), c)
        setup = next(c for c in self.box_cmds() if c["cmd"] == "echo setup-ran")
        self.assertIn("HTTPS_PROXY", setup["env"])  # installing: through the box's own proxy
        checks = next(c for c in self.box_cmds() if c["cmd"] == "echo checked")
        self.assertNotIn("HTTPS_PROXY", checks["env"])  # after that: no way out

    def test_the_way_out_is_cut_before_anything_else_runs(self):
        self.up()
        log = slurp(self.f.log).splitlines()
        cut = next(i for i, l in enumerate(log) if "--dport 8888 -j ACCEPT" in l and "iptables -D" in l)
        checked = next(i for i, l in enumerate(log) if '"cmd": "echo checked"' in l)
        self.assertLess(cut, checked)

    def test_the_box_is_deleted_at_the_end(self):
        rid, _ = self.up()
        name = self.job()["sandbox"]["box"]["name"]
        self.stop(rid)
        self.assertTrue(any(a[-1].startswith("docker rm -f %s " % name) or ("docker rm -f %s" % name) in a[-1]
                            for a in self.vm_scripts()), self.vm_scripts()[-3:])

    def test_a_command_of_the_runner_runs_in_the_box(self):
        rid, _ = self.up()
        code, res = self.review("exec", rid, "--req", "R1", "--note", "look", "--command", "echo from-runner")
        self.assertEqual(code, 0, res)
        self.assertIn("from-runner", res["output"])
        self.assertIn("echo from-runner", [c["cmd"] for c in self.box_cmds()])


class NoSandboxStopsTest(SandboxCase):
    def failed(self, mode):
        rid, _ = self.scanned()
        with mock.patch.dict(os.environ, {"STUB_LIMACTL": mode}):
            code, res = self.review("sandbox", rid)
        self.assertEqual((code, res.get("next")), (3, "ask-no-sandbox"), res)
        self.assertFalse(self.f.calls("box"))  # nothing of the merge request ran
        self.assertFalse([n for n in os.listdir(self.f.ctx().work) if n.startswith("_sandbox-")])
        return rid, res

    def missing(self, mode, words):
        rid, res = self.failed(mode)
        self.assertIn(words, res["error"])
        self.assertIn("--host", res["or_on_host"])

    def test_no_vm(self):
        self.missing("none", "does not exist")

    def test_a_stopped_vm(self):
        self.missing("stopped", "Stopped")

    def test_a_vm_not_set_up(self):
        self.missing("unprovisioned", "being set up")

    def test_a_vm_that_sees_this_machine(self):
        self.missing("mounted", "home folder")

    def test_no_lima_at_all(self):
        rid, _ = self.scanned()
        real = shutil.which
        with mock.patch.object(fx.shutil, "which", lambda n, *a, **k: None if n == "limactl" else real(n, *a, **k)):
            code, res = self.review("sandbox", rid)
        self.assertEqual(res.get("next"), "ask-no-sandbox", res)
        self.assertIn("Lima", res["error"])

    def test_on_this_machine_only_with_the_code_and_a_reason(self):
        rid, res = self.failed("none")
        with mock.patch.dict(os.environ, {"STUB_LIMACTL": "none"}):
            code, r = self.review("sandbox", rid, "--host", "--reason", "x", "--confirm", "wrong")
            self.assertEqual(code, 3, r)
            code, r = self.review("sandbox", rid, "--host", "--confirm", res["waive"])
            self.assertEqual(code, 3, r)
            code, r = self.review("sandbox", rid, "--host", "--reason", "urgent hotfix", "--confirm", res["waive"])
        self.assertEqual(code, 0, r)
        self.addCleanup(self.stop, rid)
        self.assertFalse(self.f.calls("box"))  # it ran here, as the person chose
        job = self.job()
        self.assertIsNone(job["sandbox"].get("box"))
        self.assertTrue(any("without isolation (urgent hotfix)" in n for n in job["not_checked"]), job["not_checked"])

    def test_a_box_that_cannot_be_made_stops_too(self):
        rid, res = self.failed("nobox")
        self.assertIn("could not be made", res["error"])


class AllowAndEnvTest(unittest.TestCase):
    class Ctx:
        def __init__(self, allow):
            self.allow = allow

        def c(self, key, default=None):
            return self.allow if key == "sandbox.allow" else default

    def test_the_registries_and_what_the_user_confirmed(self):
        hosts = fx.sandbox_allow(self.Ctx(["npm.mycompany.example", "*.cdn.example", "1.2.3.4", "bad host", "x"]))
        self.assertIn("registry.npmjs.org", hosts)
        self.assertIn("npm.mycompany.example", hosts)
        self.assertIn("*.cdn.example", hosts)
        self.assertNotIn("1.2.3.4", hosts)  # names only: an address is never let through
        self.assertNotIn("bad host", hosts)
        self.assertNotIn("x", hosts)
        self.assertNotIn("github.com", hosts)  # downloads only (codeload), not the site and its API

    def test_the_filter_is_anchored(self):
        f = fx.sandbox_filter(["registry.npmjs.org", "*.cdn.example"])
        self.assertTrue(re.search(f.splitlines()[0], "registry.npmjs.org"))
        self.assertFalse(re.search(f.splitlines()[0], "registry.npmjs.org.evil.example"))
        self.assertFalse(re.search(f.splitlines()[0], "registryxnpmjs.org"))
        self.assertTrue(re.search(f.splitlines()[1], "a.b.cdn.example"))
        self.assertFalse(re.search(f.splitlines()[1], "cdn.example.evil.example"))

    def test_the_box_never_gets_this_machine_s_variables(self):
        env = dict(os.environ, FACTORY_PORT="5100", PATH="/usr/bin", HOME="/Users/x", LANG="en_US.UTF-8")
        with mock.patch.dict(os.environ, {"FACTORY_PORT": "1"}):
            got = fx.box_env({"online": False}, env)
        self.assertEqual(got.get("FACTORY_PORT"), "5100")
        self.assertNotIn("PATH", got)
        self.assertNotIn("HOME", got)
        self.assertEqual(got.get("LANG"), "en_US.UTF-8")
        online = fx.box_env({"online": True, "proxy_ip": "10.0.0.2"}, {})
        self.assertEqual(online["HTTPS_PROXY"], "http://10.0.0.2:8888")


class NoWayAroundTest(unittest.TestCase):
    def test_every_command_of_a_review_goes_through_the_box(self):
        """In the review's code, a project command runs on this machine only on the path the person chose (--host)."""
        src = slurp(fx.__file__)
        body = src[src.index("def review_sandbox(ctx, a):"):src.index("def review_calls_path(")]
        for m in re.finditer(r"login_shell\(", body):
            before = body[:m.start()].splitlines()[-3:]
            self.assertTrue(any("on_host" in l or "else:" in l for l in before), "\n".join(before))
        exec_ = src[src.index("def review_exec(ctx, a):"):src.index("def review_ui(ctx, a):")]
        self.assertIn('if box.get("box"):', exec_)
        app = src[src.index("def app_start_one(ctx, job):"):src.index("def box_app_start(")]
        self.assertLess(app.index("if box:"), app.index("subprocess.Popen(login_shell("))

    def test_every_project_command_of_a_job_goes_through_its_box(self):
        """A job's setup, checks, e2e, seed, login and permission-table commands reach this machine only through
        proj_run (or the box test right before), which runs them in the job's box when it has one."""
        src = slurp(fx.__file__)

        def body(name):
            i = src.index("def %s(" % name)
            return src[i:src.index("\ndef ", i + 1)]
        for name in ("cmd_setup", "perm_cmd", "login_run"):
            self.assertNotIn("login_shell(", body(name), name)
            self.assertIn("proj_run(", body(name), name)
        for name in ("gate_round_one", "gate_round_repos"):
            self.assertNotIn('login_shell(rc_, app_fill(rc_.c("e2e")', body(name))
            self.assertNotIn('login_shell(ctx, app_fill(ctx.c("e2e")', body(name))
        for name in ("checks_execute", "run_seed"):
            b = body(name)
            self.assertLess(b.index("job_box_of("), b.index('run(["bash"'), name)
        run_ = body("cmd_run")
        self.assertLess(run_.index("if box:"), run_.index("subprocess.Popen(cmd"))


class JobBoxTest(FxTestCase):
    """A job's own commands run in its own box (3.8): setup and installs with the registries only, everything else
    without; a job waiting for a person has its box stopped; the box goes with the job's clones; no sandbox stops the
    job before its setup."""
    cfg = {"setup": "echo setup-ran"}

    def setUp(self):
        super().setUp()
        self.rundir = os.path.join(self.tmp, "stubrun")
        os.makedirs(self.rundir)
        p = mock.patch.dict(os.environ, {"FACTORY_TEST_JOB_BOX": "1", "STUB_RUN_DIR": self.rundir})
        p.start()
        self.addCleanup(p.stop)
        write(os.path.join(self.f.factory, "checks.sh"), "echo checks-ran\nexit 0\n")

    def box_cmds(self):
        return self.f.calls("box")

    def vm(self, what=None):
        return [c for c in self.f.calls("boxvm") if what is None or c["what"] == what]

    def raw(self, *argv):
        """An fx command whose output is the command's own (fx run): (exit code, stdout)."""
        buf, code = io.StringIO(), 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                fx.main(["--repo", self.f.factory] + list(argv))
            except SystemExit as e:
                code = e.code or 0
        os.chdir(self.cwd)
        return code, buf.getvalue()

    def records(self):
        return fx.job_box_records(self.f.ctx(), "T-1")

    def setup_job(self):
        code, res = self.fx("setup", "T-1", "--force")
        self.assertEqual(code, 0, res)
        return res

    def test_setup_runs_in_the_job_s_box_with_the_registries(self):
        self.setup_job()
        setup = [c for c in self.box_cmds() if c["cmd"] == "echo setup-ran"]
        self.assertEqual(len(setup), 1, self.box_cmds())
        self.assertIn("HTTPS_PROXY", setup[0]["env"])  # an install: through the box's proxy
        self.assertFalse({"PATH", "HOME"} & set(setup[0]["env"]))
        self.assertEqual(len(self.records()), 1)
        name = self.records()[0][1]["name"]
        self.assertTrue(name.startswith("fxbox-%s-t-1-" % fx.box_factory_id(self.f.ctx())), name)
        self.assertFalse(self.records()[0][1]["online"])  # cut after the install
        self.assertTrue(self.vm("cut"))

    def test_a_command_of_the_agents_runs_in_the_box_without_the_registries(self):
        self.setup_job()
        code, text = self.raw("run", "--job", "T-1", "--", "echo", "hello box")
        self.assertEqual(code, 0, text)
        self.assertIn("hello box", text)
        run_ = [c for c in self.box_cmds() if c["cmd"] == "echo 'hello box'"]
        self.assertEqual(len(run_), 1, self.box_cmds())
        self.assertNotIn("HTTPS_PROXY", run_[0]["env"])
        self.assertIn("FACTORY_PORT", run_[0]["env"])

    def test_an_install_alone_reaches_the_registries(self):
        self.setup_job()
        opens = len(self.vm("open"))
        code, text = self.raw("run", "--job", "T-1", "--install", "--", "echo", "npm-install-x")
        self.assertEqual(code, 0, text)
        inst = [c for c in self.box_cmds() if c["cmd"] == "echo npm-install-x"]
        self.assertIn("HTTPS_PROXY", inst[0]["env"])
        self.assertEqual(len(self.vm("open")), opens + 1)
        self.assertFalse(self.records()[0][1]["online"])
        code, text = self.raw("run", "--install", "--", "echo", "x")
        self.assertNotEqual(code, 0)  # --install is for a job's box

    def test_the_gate_s_checks_run_in_the_box(self):
        self.setup_job()
        self.f.commit({"app.py": "x = 1\n"})
        self.gate()
        checks = [c for c in self.box_cmds() if c["cmd"].startswith("bash ") and c["cmd"].endswith("checks.sh")]
        self.assertTrue(checks, [c["cmd"] for c in self.box_cmds()])
        self.assertNotIn("HTTPS_PROXY", checks[0]["env"])
        ins = [c for c in self.f.calls("boxsync") if c["dir"] == "in"]
        self.assertTrue(any(os.path.join(self.f.wt, "app.py") in c["files"] for c in ins))

    def test_what_goes_in_is_the_clone_the_job_and_the_factory_s_own_files(self):
        self.setup_job()
        write(os.path.join(self.f.factory, "jobs", "T-2", "job.json"), "{}")
        write(os.path.join(self.f.factory, "local", "secret.json"), "{}")
        write(os.path.join(self.f.jd, "ui", "seed.sh"), "true\n")
        got = fx.box_listing(self.f.ctx(), fx.job_flat_copy(json.loads(slurp(os.path.join(self.f.jd, "job.json")))))
        self.assertIn(os.path.join(self.f.jd, "ui", "seed.sh"), got)
        self.assertIn(os.path.join(self.f.factory, "factory.json"), got)
        self.assertFalse([p for p in got if "/jobs/T-2/" in p or "/local/" in p or "/.git/" in p])

    def test_a_waiting_job_s_box_stops_and_starts_again(self):
        self.setup_job()
        code, res = self.fx("stage", "T-1", "needs-info")
        self.assertEqual(code, 0, res)
        self.assertEqual(len(self.vm("pause")), 1)
        code, res = self.fx("stage", "T-1", "build")
        code, text = self.raw("run", "--job", "T-1", "--", "echo", "back")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.vm("resume")), 1)
        self.assertEqual(len(self.vm("create")), 1)  # the same box: what it installed is still there

    def test_the_box_goes_with_the_job_s_clones(self):
        self.setup_job()
        name = self.records()[0][1]["name"]
        code, res = self.fx("cleanup", "T-1", "--force")
        self.assertEqual(code, 0, res)
        self.assertIn(name, [c["name"] for c in self.vm("destroy")])
        self.assertFalse(self.records())

    def test_sandbox_clean_keeps_what_jobs_use_and_other_factories_boxes(self):
        self.setup_job()
        live = self.records()[0][1]["name"]
        mine = "fxbox-%s-old-abc123" % fx.box_factory_id(self.f.ctx())
        other = "fxbox-zzzzzz-t-1-abc123"
        for n in (mine, other):
            write(os.path.join(self.rundir, "box-" + n), "true")
        code, res = self.fx("sandbox", "clean")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["removed"], [mine])
        self.assertNotIn(live, res["removed"])

    def test_no_sandbox_stops_the_job_before_its_setup(self):
        with mock.patch.dict(os.environ, {"STUB_LIMACTL": "none"}):
            code, res = self.fx("setup", "T-1", "--force")
            self.assertEqual((code, res.get("next")), (3, "ask-no-sandbox"), res)
            self.assertFalse(self.box_cmds())
            self.assertFalse(os.path.exists(os.path.join(self.f.out, "setup-ran")))
            code, r = self.fx("sandbox", "host", "--job", "T-1", "--reason", "no VM on this laptop", "--confirm", "x")
            self.assertEqual(code, 3, r)
            code, r = self.fx("sandbox", "host", "--job", "T-1", "--confirm", res["waive"])
            self.assertEqual(code, 3, r)
            code, r = self.fx("sandbox", "host", "--job", "T-1", "--reason", "no VM on this laptop", "--confirm",
                              res["waive"])
            self.assertEqual(code, 0, r)
            code, r = self.fx("setup", "T-1", "--force")
            self.assertEqual(code, 0, r)
        self.assertFalse(self.box_cmds())  # on this machine, on the person's word
        notes = fx.job_isolation_notes(self.f.ctx(), self.f.job())
        self.assertTrue(any("no VM on this laptop" in n for n in notes), notes)


class SyncBackTest(FxTestCase):
    """What a command changed in the box comes back to the clone: only files git sees, never a link, never what this
    machine or its agents would run by themselves (decision J6)."""

    def test_only_safe_changes_come_back(self):
        self.f.commit({".gitignore": "node_modules/\n", "old.txt": "old\n", "package.json": "{}\n"})
        wt = self.f.wt
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(wt, "linkdir"))
        os.symlink(os.path.join(outside, "bashrc"), os.path.join(wt, "linkfile"))
        ctx = self.f.ctx()
        box = {"name": "fxbox-test-t-1-aaaaaa"}
        view = {"id": "T-1", "worktree": wt}
        mf = fx.box_state_path(ctx, box["name"], ".files.json")
        write(mf, json.dumps({os.path.join(wt, "old.txt"): [4, 1]}))
        changed = {"package-lock.json": b"lock\n", ".claude/settings.json": b"{}", "node_modules/x/index.js": b"x",
                   "linkdir/evil": b"evil", "linkfile": b"not through the link", ".envrc": b"bad", "sub/new.txt": b"n"}
        listing = b"".join(b"M" + os.path.join(wt, k).encode() + b"\0" for k in changed)
        listing += b"D" + os.path.join(wt, "old.txt").encode() + b"\0"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for k, v in changed.items():
                ti = tarfile.TarInfo(os.path.join(wt, k).lstrip("/"))
                ti.size = len(v)
                t.addfile(ti, io.BytesIO(v))
        answers = iter([mock.Mock(returncode=0, stdout=listing), mock.Mock(returncode=0, stdout=buf.getvalue())])
        asked = []

        def vm_run(argv, data=None, **kw):
            asked.append((argv, data))
            return next(answers)
        with mock.patch.object(fx, "vm_run", vm_run):
            came = fx.box_sync_out(ctx, view, box, "/run/fx-stamp-x")
        self.assertEqual(slurp(os.path.join(wt, "package-lock.json")), "lock\n")
        self.assertEqual(slurp(os.path.join(wt, "sub", "new.txt")), "n")
        self.assertFalse(os.path.exists(os.path.join(wt, ".claude", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(wt, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(wt, ".envrc")))
        self.assertFalse(os.listdir(outside))  # nothing written through a link
        self.assertFalse(os.path.islink(os.path.join(wt, "linkfile")))
        self.assertEqual(slurp(os.path.join(wt, "linkfile")), "not through the link")
        self.assertFalse(os.path.exists(os.path.join(wt, "old.txt")))
        fetched = asked[1][1].decode().split("\0")
        self.assertNotIn(os.path.join(wt, "node_modules/x/index.js").lstrip("/"), fetched)
        self.assertNotIn(os.path.join(wt, ".claude/settings.json").lstrip("/"), fetched)
        self.assertIn(os.path.join(wt, "package-lock.json"), came)


class KeyAdviceTest(unittest.TestCase):
    def test_a_long_lived_default_key_is_said(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        write(os.path.join(home, ".aws", "credentials"), "[default]\naws_access_key_id = AKIAFAKEFAKEFAKE\n"
                                                         "aws_secret_access_key = FAKE\n")
        with mock.patch.dict(os.environ, {"HOME": home}):
            said = fx.aws_key_advice(object())
        self.assertTrue(any("long-lived key" in s for s in said), said)
        self.assertFalse(any("FAKE" in s for s in said))

    def test_the_deny_rules_cover_the_machine_s_keys(self):
        for rule in ("Read(~/.aws/**)", "Read(~/.ssh/**)", "Bash(*.aws/*)"):
            self.assertIn(rule, fx.LOCAL_DENY)


@unittest.skipUnless(os.environ.get("FX_SANDBOX_REAL") == "1" and shutil.which("limactl"),
                     "the real sandbox VM: FX_SANDBOX_REAL=1 after fx sandbox install")
class RealSandboxTest(unittest.TestCase):
    """The review's proof of concept, in a real box: no key of this machine, no way out once installed."""

    def setUp(self):
        ok, why = fx.sandbox_state()
        self.assertTrue(ok, why)
        self.box = fx.box_create(None, "isolation-test", 25900, 2)
        self.addCleanup(fx.box_destroy, self.box)

    def run_in(self, cmd):
        return fx.box_exec(self.box, cmd, "/", {}, timeout=300)

    def test_no_key_of_this_machine(self):
        p = self.run_in("cat ~/.aws/credentials ~/.ssh/id_* %s/*/s3-*.env 2>&1; ls %s 2>&1; ls /var/run/docker.sock "
                        "2>&1; true" % (fx.keys_root(), os.path.expanduser("~")))
        self.assertNotIn("aws_secret_access_key", p.stdout)
        self.assertIn("No such file", p.stdout)

    def test_only_the_registries_while_installing_then_nothing(self):
        p = self.run_in("curl -sS -o /dev/null -w '%{http_code}' https://registry.npmjs.org/ 2>&1")
        self.assertIn("200", p.stdout)
        p = self.run_in("curl -sS -m 10 https://example.com/ 2>&1; echo exit=$?")
        self.assertNotIn("exit=0", p.stdout)
        fx.box_cut(self.box)
        p = self.run_in("curl -sS -m 10 https://registry.npmjs.org/ 2>&1; echo exit=$?")
        self.assertNotIn("exit=0", p.stdout)
        p = self.run_in("getent hosts github.com >/dev/null && echo dns-ok || echo no-dns")
        self.assertIn("no-dns", p.stdout)

    def test_a_malicious_postinstall_finds_nothing(self):
        p = self.run_in("mkdir -p /work && cd /work && printf '%s' '{\"name\":\"x\",\"version\":\"1.0.0\",\"scripts\":"
                        "{\"postinstall\":\"cat $HOME/.aws/credentials > leak.txt 2>&1; true\"}}' > package.json && "
                        "npm install --no-audit --no-fund >/dev/null 2>&1; cat leak.txt")
        self.assertNotIn("aws_secret_access_key", p.stdout)


if __name__ == "__main__":
    unittest.main()
