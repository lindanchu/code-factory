#!/usr/bin/env python3
"""A colleague's merge request runs in its review's box, in the sandbox VM, never on this machine: its setup, app,
checks, commands and browser get only what fx passes them, the box loses its way out once installed, is deleted at the
end, and a machine without the sandbox stops the review before anything of the merge request runs.

The first tests use the limactl stub (test_security.STUB): what the box runs runs here, with only the variables fx
gave the box, and every command is logged. The Real* ones need the real VM: FX_SANDBOX_REAL=1 with `fx sandbox install`
done; they run the review's proof of concept against it, a job's box, and a whole review and job through fx."""

import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
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

    def starts(self):
        return [c for c in self.f.calls("limactl") if c["argv"][:1] == ["start"]]

    def test_a_stopped_vm_is_started_and_the_review_runs(self):
        rid, _ = self.scanned()  # the machine restarted: the VM is there, stopped
        with mock.patch.dict(os.environ, {"STUB_LIMACTL": "stopped"}), \
                mock.patch.object(fx, "sandbox_kvm", return_value=""):
            code, res = self.review("sandbox", rid)
        self.assertEqual(code, 0, res)
        self.addCleanup(self.stop, rid)
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue(self.f.calls("box"))  # it ran in the box, as with a running VM

    def test_a_stopped_vm_that_does_not_start(self):
        with mock.patch.object(fx, "sandbox_kvm", return_value=""):
            rid, res = self.failed("stopped-nostart")
        self.assertIn("Stopped", res["error"])
        self.assertIn("limactl start failed (exit 1): FATA[0001] exiting, status=Broken", res["error"])

    def test_a_stopped_vm_this_terminal_cannot_start(self):
        with mock.patch.object(fx, "sandbox_kvm", return_value="this terminal cannot use /dev/kvm: a new terminal"):
            rid, res = self.failed("stopped")
        self.assertIn("Stopped", res["error"])
        self.assertIn("fx cannot start it from here: this terminal cannot use /dev/kvm", res["error"])
        self.assertFalse(self.starts())

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


class KvmTest(unittest.TestCase):
    """Lima's QEMU on Linux and WSL needs /dev/kvm, and a terminal opened before the user joined its group has not."""

    def kvm(self, platform, exists, usable):
        with mock.patch.object(fx.sys, "platform", platform), \
                mock.patch.object(fx.os.path, "exists", lambda p: exists if p == "/dev/kvm" else os.path.lexists(p)), \
                mock.patch.object(fx.os, "access", lambda p, m: usable if p == "/dev/kvm" else True):
            return fx.sandbox_kvm()

    def test_macos_needs_none(self):
        self.assertEqual(self.kvm("darwin", False, False), "")

    def test_linux_with_kvm(self):
        self.assertEqual(self.kvm("linux", True, True), "")

    def test_no_kvm_says_nested_virtualization(self):
        self.assertIn("nestedVirtualization", self.kvm("linux", False, False))

    def test_a_terminal_without_the_group_says_a_new_one(self):
        why = self.kvm("linux", True, False)
        self.assertIn("usermod -aG kvm", why)
        self.assertIn("new terminal", why)

    def test_the_install_steps_cover_linux_and_wsl(self):
        steps = " ".join(fx.SANDBOX_INSTALL["Linux and WSL"])
        for words in ("qemu-system-x86", "lima-vm/lima/releases", "SHA256SUMS", "kvm", "wsl --shutdown"):
            self.assertIn(words, steps)
        self.assertIn("starts the VM again", fx.SANDBOX_INSTALL["after a restart"])


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

    def test_a_restarted_vm_is_started_and_the_job_keeps_its_box(self):
        self.setup_job()
        name = self.records()[0][1]["name"]
        write(os.path.join(self.rundir, "box-" + name), "false")  # the VM stopped with the machine, and its boxes
        with mock.patch.dict(os.environ, {"STUB_LIMACTL": "stopped"}), \
                mock.patch.object(fx, "sandbox_kvm", return_value=""):
            code, text = self.raw("run", "--job", "T-1", "--", "echo", "back")
        self.assertEqual(code, 0, text)
        self.assertEqual(len([c for c in self.f.calls("limactl") if c["argv"][:1] == ["start"]]), 1)
        self.assertEqual(len(self.vm("resume")), 1)
        self.assertEqual(len(self.vm("create")), 1)  # the same box: what it installed is still there
        self.assertEqual(self.records()[0][1]["name"], name)

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



class FakeCtx:
    """What the job box's copying reads of a factory: its folders, no repositories of its own."""

    def __init__(self, root):
        self.factory, self.work = os.path.join(root, "factory"), os.path.join(root, "work")
        self.jobs = os.path.join(self.factory, "jobs")
        os.makedirs(self.jobs)
        os.makedirs(self.work)

    def job_dir(self, jid):
        return os.path.join(self.jobs, jid)

    def repo_names(self):
        return []

    def c(self, key, default=None):
        return default

    def rc(self, name, key, default=None):
        return default


@unittest.skipUnless(os.environ.get("FX_SANDBOX_REAL") == "1" and shutil.which("limactl"),
                     "the real sandbox VM: FX_SANDBOX_REAL=1 after fx sandbox install")
class RealJobBoxTest(unittest.TestCase):
    """A job's box in the real VM (3.8): an install opens the way to the registries and closes it, a stopped box keeps
    what it had and starts with no way out, files go in and only the safe ones come back, a time limit holds inside,
    and an app in the box answers on this machine's 127.0.0.1."""

    def setUp(self):
        ok, why = fx.sandbox_state()
        self.assertTrue(ok, why)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-jobbox-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.box = fx.box_create(None, "jobbox-test", 25950, 2)
        self.addCleanup(fx.box_destroy, self.box)
        fx.box_cut(self.box)

    def run_in(self, cmd, cwd="/", timeout=300):
        return fx.box_exec(self.box, cmd, cwd, {}, timeout=timeout)

    def reach(self, url):
        return self.run_in("curl -sS -m 15 -o /dev/null -w '%%{http_code}' %s 2>&1; echo \" exit=$?\"" % url).stdout

    def test_an_install_alone_reaches_the_registries(self):
        self.assertNotIn("exit=0", self.reach("https://registry.npmjs.org/"))
        fx.box_open(self.box)
        self.assertIn("200 exit=0", self.reach("https://registry.npmjs.org/"))
        self.assertNotIn("exit=0", self.reach("https://example.com/"))
        fx.box_cut(self.box)
        self.assertNotIn("exit=0", self.reach("https://registry.npmjs.org/"))
        fx.box_open(self.box)  # a second install: a new proxy, found again
        self.assertIn("200 exit=0", self.reach("https://registry.npmjs.org/"))
        fx.box_cut(self.box)

    def test_its_own_docker_pulls_images_only_while_installing(self):
        """A compose stack's images (Docker Hub's layers come from its CDN) come down during an install, not after."""
        fx.box_open(self.box)
        p = self.run_in("docker pull -q hello-world:latest 2>&1; echo exit=$?", timeout=600)
        self.assertIn("exit=0", p.stdout)
        fx.box_cut(self.box)
        p = self.run_in("docker pull -q busybox:latest 2>&1; echo exit=$?", timeout=600)
        self.assertNotIn("exit=0", p.stdout)

    def test_a_stopped_box_keeps_its_files_and_starts_with_no_way_out(self):
        self.run_in("mkdir -p /work && echo kept > /work/keep.txt")
        fx.box_pause(self.box)
        self.assertIs(fx.box_running(self.box), False)
        fx.box_resume(self.box)
        self.assertIs(fx.box_running(self.box), True)
        self.assertIn("kept", self.run_in("cat /work/keep.txt").stdout)
        self.assertNotIn("exit=0", self.reach("https://registry.npmjs.org/"))

    def test_files_go_in_and_only_safe_ones_come_back(self):
        ctx = FakeCtx(self.tmp)
        wt = os.path.join(ctx.work, "T-9")
        os.makedirs(wt)
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@example.com"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=wt, check=True)
        write(os.path.join(wt, ".gitignore"), "node_modules/\n")
        write(os.path.join(wt, "a.txt"), "a\n")
        write(os.path.join(wt, "src", "app.js"), "1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-qm", "x"], cwd=wt, check=True)
        view = {"id": "T-9", "worktree": wt}
        stamp = fx.box_sync_in(ctx, view, self.box)
        p = self.run_in("cat src/app.js && echo lock > package-lock.json && mkdir -p node_modules/x .claude && "
                        "echo x > node_modules/x/i.js && echo bad > .claude/settings.json && rm a.txt && "
                        "ln -s /etc/passwd link && echo new > src/new.js", cwd=wt)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("1", p.stdout)  # the clone's file went in
        came = fx.box_sync_out(ctx, view, self.box, stamp)
        self.assertEqual(slurp(os.path.join(wt, "package-lock.json")), "lock\n")
        self.assertEqual(slurp(os.path.join(wt, "src", "new.js")), "new\n")
        self.assertFalse(os.path.exists(os.path.join(wt, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(wt, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(wt, ".claude")))
        self.assertFalse(os.path.lexists(os.path.join(wt, "link")))
        self.assertIn(os.path.join(wt, "package-lock.json"), came)
        write(os.path.join(wt, "src", "app.js"), "2\n")  # a change here goes in at the next command
        stamp = fx.box_sync_in(ctx, view, self.box)
        self.assertIn("2", self.run_in("cat src/app.js", cwd=wt).stdout)
        fx.box_sync_out(ctx, view, self.box, stamp)

    def test_the_time_limit_holds_inside_the_box(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_in("sleep 120", timeout=3)
        time.sleep(20)  # timeout -k 15: TERM, then KILL
        self.assertIn("none", self.run_in("pgrep -x sleep || echo none").stdout)

    def test_an_app_in_the_box_answers_here(self):
        port = self.box["port"]
        fx.box_spawn(self.box, "node -e \"require('http').createServer((q, r) => r.end('ok')).listen(%d, "
                               "'127.0.0.1')\"" % port, "/", {}, "/tmp/fx-app.log", "app-test")
        for _ in range(60):
            if fx.http_up("http://127.0.0.1:%d/" % port):
                break
            time.sleep(1)
        self.assertTrue(fx.http_up("http://127.0.0.1:%d/" % port))
        fx.box_kill(self.box, "app-test")



LIMACTL = shutil.which("limactl")  # before a test's own PATH
SERVER_JS = """const http = require('http');
http.createServer((q, r) => { r.end(q.url === '/health' ? 'ok' : 'app'); }).listen(Number(process.argv[2]), '127.0.0.1');
"""


@unittest.skipUnless(os.environ.get("FX_SANDBOX_REAL") == "1" and shutil.which("limactl"),
                     "the real sandbox VM: FX_SANDBOX_REAL=1 after fx sandbox install")
class RealJobFlowTest(FxTestCase):
    """A job through fx in the real VM: its setup installs a real package through the box's proxy, an agent's command
    runs with no internet, an install alone gets the registries and its lockfile comes back, the app answers here, a
    waiting job's box stops and starts again with what it had, and the box goes with the job's clones."""
    tools = ("gitleaks", "semgrep", "osv-scanner", "docker")
    cfg = {"setup": "npm init -y >/dev/null && npm install --no-audit --no-fund is-number@7.0.0",
           "app": {"up": "node server.js {port}", "health": "/health", "start_timeout_sec": 60},
           "ports": {"base": 25300, "step": 10}}

    def setUp(self):
        super().setUp()
        os.symlink(LIMACTL, os.path.join(self.f.bin, "limactl"))
        p = mock.patch.dict(os.environ, {"FACTORY_TEST_JOB_BOX": "1"})
        p.start()
        self.addCleanup(p.stop)
        write(os.path.join(self.f.factory, "checks.sh"), "echo checks-ran-in-$(hostname)\nexit 0\n")
        self.f.commit({".gitignore": "node_modules/\n", "server.js": SERVER_JS})
        self.f.save_job(port=25300)
        self.addCleanup(self.fx, "cleanup", "T-1", "--force")

    def raw(self, *argv):
        buf, code = io.StringIO(), 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                fx.main(["--repo", self.f.factory] + list(argv))
            except SystemExit as e:
                code = e.code or 0
        os.chdir(self.cwd)
        return code, buf.getvalue()

    def box(self):
        return fx.job_box_records(self.f.ctx(), "T-1")[0][1]

    def test_a_job_from_setup_to_cleanup(self):
        code, res = self.fx("setup", "T-1", "--force")
        self.assertEqual(code, 0, res)
        wt = self.f.wt
        self.assertTrue(os.path.exists(os.path.join(wt, "package-lock.json")))  # came back
        self.assertFalse(os.path.exists(os.path.join(wt, "node_modules")))  # stayed in the box
        code, text = self.raw("run", "--job", "T-1", "--", "node", "-e", "console.log(require('is-number')(5))")
        self.assertEqual((code, text.strip()), (0, "true"))
        code, text = self.raw("run", "--job", "T-1", "--", "curl", "-sS", "-m", "10", "https://registry.npmjs.org/")
        self.assertNotEqual(code, 0, text)  # no internet outside an install
        code, text = self.raw("run", "--job", "T-1", "--", "cat", os.path.expanduser("~/.ssh/known_hosts"))
        self.assertNotEqual(code, 0, text)  # nothing of this machine's home
        code, text = self.raw("run", "--job", "T-1", "--install", "--", "npm", "install", "--no-audit", "--no-fund",
                              "is-odd@3.0.1")
        self.assertEqual(code, 0, text)
        self.assertIn("is-odd", slurp(os.path.join(wt, "package.json")))
        code, res = self.fx("app", "up", "T-1")
        self.assertEqual(code, 0, res)
        self.assertTrue(fx.http_up("http://127.0.0.1:25300/health"))
        code, res = self.fx("app", "down", "T-1")
        self.assertEqual(code, 0, res)
        # the builder commits what the installs changed, as on this machine before
        self.f.commit({"app.py": "x = 1\n", "package.json": slurp(os.path.join(wt, "package.json")),
                       "package-lock.json": slurp(os.path.join(wt, "package-lock.json"))})
        code, res = self.gate()
        self.assertNotIn("Untracked", slurp(res["report"]))
        logs = [slurp(os.path.join(d, f)) for d, _, fs in os.walk(self.f.jd) for f in fs if f.endswith(".log")]
        self.assertTrue(any("checks-ran-in-box" in t for t in logs), "the checks did not run in the box")
        code, res = self.fx("stage", "T-1", "needs-info", "--force")
        self.assertEqual(code, 0, res)
        self.assertIs(fx.box_running(self.box()), False)
        self.fx("stage", "T-1", "build", "--force")
        code, text = self.raw("run", "--job", "T-1", "--", "node", "-e", "console.log(require('is-odd')(3))")
        self.assertEqual((code, text.strip()), (0, "true"))  # what it installed is still there
        box = self.box()
        code, res = self.fx("cleanup", "T-1", "--force")
        self.assertEqual(code, 0, res)
        self.assertIsNone(fx.box_running(box))



@unittest.skipUnless(os.environ.get("FX_SANDBOX_REAL") == "1" and shutil.which("limactl"),
                     "the real sandbox VM: FX_SANDBOX_REAL=1 after fx sandbox install")
class RealReviewFlowTest(SandboxCase):
    """A review through fx in the real VM: the merge request's app starts in its box and answers here, the runner's
    command runs there with nothing of this machine, and the box is gone at the end."""
    tools = ("gitleaks", "semgrep", "osv-scanner", "docker")

    def setUp(self):
        super().setUp()
        os.symlink(LIMACTL, os.path.join(self.f.bin, "limactl"))
        server = os.path.join(self.f.factory, "server.js")  # the box gets the factory's own files
        write(server, SERVER_JS)
        self.set_app(up="node %s {port}" % server, health="/health")

    def test_a_review_from_its_sandbox_to_its_end(self):
        rid, res = self.up()
        self.assertTrue(fx.http_up(res["url"] + "/health"), res)
        code, r = self.review("exec", rid, "--req", "R1", "--note", "look", "--command",
                              "hostname; ls %s 2>&1; curl -sS -m 10 https://registry.npmjs.org/ 2>&1; echo exit=$?"
                              % os.path.expanduser("~/.ssh"))
        self.assertEqual(code, 0, r)
        self.assertIn("box", r["output"])
        self.assertIn("No such file", r["output"])
        self.assertNotIn("exit=0", r["output"])
        box = self.job()["sandbox"]["box"]
        self.stop(rid)
        self.assertIsNone(fx.box_running(box))


@unittest.skipUnless(os.environ.get("FX_SANDBOX_REAL") == "1" and os.environ.get("FACTORY_TEST_TAURI") == "1"
                     and shutil.which("limactl"),
                     "the real sandbox VM and a real Tauri build on Windows: FX_SANDBOX_REAL=1 FACTORY_TEST_TAURI=1, in WSL")
class RealDesktopReviewTest(SandboxCase):
    """A desktop app's merge request through fx, all of it real: its API and website start in the review's box, the
    website and the app's window are checked by the box's browser, then its real Tauri build is made on Windows, pointed
    at the box's API, and its check calls that API across the sandbox. The review says the real build was not isolated,
    and the box is gone at the end."""
    tools = ("gitleaks", "semgrep", "osv-scanner", "docker")
    APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop-fixture", "api-app")

    def setUp(self):
        from test_desktop import D, ico_32, node_modules
        host = D.WinHost()
        why = (None if host.wsl else "not in WSL") or host.runner()[1] or \
            (None if host.cargo() else "no Rust on Windows") or (None if host.msvc() else "no MSVC build tools on Windows") \
            or (None if node_modules() and shutil.which("node") else "no Node and Playwright for the factory here")
        if why:
            self.skipTest(why)
        # Windows' folders, while this machine's PATH still has cmd.exe
        win_home, app_data = host.home(), host.to_wsl(host.win_var("LOCALAPPDATA"))
        # the made-up machine's PATH gets Lima, this machine's Node and Playwright, and what fx runs on Windows (node.exe is
        # found where its installer put it)
        progs = [("limactl", LIMACTL), ("node", shutil.which("node"))] + \
            [(n, shutil.which(n)) for n in ("cmd.exe", "powershell.exe", "netstat.exe")]
        modules = node_modules()
        super().setUp()
        for name, path in progs:
            os.symlink(path, os.path.join(self.f.bin, name))
        # the real app's check runs here, with the factory's Playwright (the box has its own)
        os.makedirs(os.environ["FACTORY_TOOLS_DIR"], exist_ok=True)
        os.symlink(modules, os.path.join(os.environ["FACTORY_TOOLS_DIR"], "node_modules"))
        self.set_app(up="node api/server.js {port}", health="/health")
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["desktop"] = {"framework": "tauri", "dir": "src-tauri", "build": {"windows": "cd src-tauri && cargo build"},
                          "build_timeout_sec": 3000, "start_timeout_sec": 120}
        cfg["e2e_timeout_sec"] = 900
        write(path, json.dumps(cfg))
        # the merge request is the app: its files, and the icon Tauri's build wants
        files = {}
        for d, _, fs in os.walk(self.APP):
            for f in fs:
                files[os.path.relpath(os.path.join(d, f), self.APP)] = slurp(os.path.join(d, f))
        os.makedirs(os.path.join(self.f.wt, "src-tauri", "icons"), exist_ok=True)
        with open(os.path.join(self.f.wt, "src-tauri", "icons", "icon.ico"), "wb") as f:
            f.write(ico_32())
        subprocess.run(["git", "add", "src-tauri/icons/icon.ico"], cwd=self.f.wt, check=True)
        self.head = self.push_mr(files, "the app, its API and its window")
        self.set_mr()
        # its copy and Rust's target on Windows (about 2 GB), and what WebView2 keeps for it: gone after the test
        key = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(self.f.factory))
        self.addCleanup(lambda: [shutil.rmtree(p, True) for d in ("src", "builds")
                                 for p in glob.glob(os.path.join(win_home, d, key + "-*"))])
        self.addCleanup(lambda: shutil.rmtree(os.path.join(app_data, "test.factory.apiapp"), True))

    def test_the_api_in_the_box_answers_the_real_app_on_windows(self):
        rid, res = self.up()
        self.assertTrue(fx.http_up(res["url"] + "/health"), res)
        code, r = self.call(rid, "normal", "R1", "/api/greeting")
        self.assertEqual(code, 0, r)
        code, r = self.review("exec", rid, "--req", "R1", "--note", "where", "--command",
                              "ls %s 2>&1; echo exit=$?" % os.path.expanduser("~/.ssh"))
        self.assertIn("No such file", r["output"])  # the API's box sees nothing of this machine
        script = os.path.join(self.tmp, "check.js")
        shutil.copyfile(os.path.join(self.APP, "check.js"), script)
        t0 = time.time()
        code, r = self.review("ui", rid, "--req", "R1", "--case", "website, window and the real app", "--script", script)
        print("\n[review ui: %ds]" % (time.time() - t0))
        self.assertEqual(code, 0, r)
        rep = load_json(os.path.join(os.path.dirname(r["report"]), "ui-report.json"))
        print(json.dumps({"steps": [(s["name"], s["ok"]) for s in rep.get("steps", [])],
                          "real": (rep.get("desktop") or {}).get("real")}, indent=1))
        applog = fx.read(os.path.join(os.path.dirname(r["report"]), "ui-app.log"), "") or ""
        print("[ui-app.log]\n" + applog[-3000:])
        self.assertIsNone(rep.get("error"), rep.get("error"))
        self.assertEqual([s["name"] for s in rep["steps"] if not s["ok"]], [])
        names = [s["name"] for s in rep["steps"]]
        for kind in ("web", "desktop", "app"):  # in the box's browser twice, then the real app on Windows
            self.assertTrue(any(n.endswith("%s: the API answered" % kind) for n in names), names)
        real = rep["desktop"]["real"]
        self.assertFalse(real.get("skipped") or real.get("build_error") or real.get("error"), real)
        self.assertTrue(any("real build ran on the reviewer's Windows, not in the sandbox" in n
                            for n in self.job()["not_checked"]), self.job()["not_checked"])
        box = self.job()["sandbox"]["box"]
        self.stop(rid)
        self.assertIsNone(fx.box_running(box))


if __name__ == "__main__":
    unittest.main()
