"""Several sessions at once: what fx hands out, what it lets run together, and what one job's command may do to another's.

Standard library only. Run from the repository root:
    python3 -I -m unittest discover -s tests

Two kinds of test. The first builds throw-away git repositories (a bare project, a factory repository made by `fx init`)
and runs fx in separate processes, as separate Claude Code sessions do: nothing may be handed out twice and no command
may wipe what another wrote. The second tests the line for room on the machine. Its machine is a JSON file, so the
numbers are the same wherever the tests run, and every step is its own `fx run` process that says in a file what it
uses, which fx reads as its measurement.
"""

import copy
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from test_security import FX_PATH, SECRET_ENV, STUB, fx

JIRA = "https://example.atlassian.net"
PORT_BASE = 45100  # away from the ports of real jobs on the machine that runs the tests
GITIGNORE = "jobs/\nbaseline.json\nlocal/\n.claude/settings.local.json\n"
SLOW_CHECKS = "#!/usr/bin/env bash\nsleep %s\nexit 0\n"
PLAN = """PLAN: SPLIT

```json
{"packages": [
  {"id": "wp0", "title": "contract", "owns": ["src/a.py"], "contract": true},
  {"id": "wp1", "title": "back end", "owns": ["src/b.py"], "after": ["wp0"]},
  {"id": "wp2", "title": "front end", "owns": ["web/*"], "after": ["wp0"]}
]}
```
"""
WORKER = r'''
import json, os, sys, time
tag, seconds, log = sys.argv[1], float(sys.argv[2]), sys.argv[3]
use = dict(kv.split("=") for kv in sys.argv[4:])
folder = os.path.join(os.environ["FACTORY_STATE_DIR"], "run", "fake-usage")
os.makedirs(folder, exist_ok=True)
usage = os.path.join(folder, os.environ["FACTORY_PLACE"] + ".json")
with open(os.path.join(os.path.dirname(log), "pid-" + tag), "w") as f:
    f.write(str(os.getpid()))
with open(log, "a") as f:
    f.write("start %s %.3f\n" % (tag, time.time()))
with open(usage + ".tmp", "w") as f:
    json.dump({k: float(v) for k, v in use.items()}, f)
os.replace(usage + ".tmp", usage)
try:
    time.sleep(seconds)
finally:
    os.remove(usage)
    with open(log, "a") as f:
        f.write("end %s %.3f\n" % (tag, time.time()))
'''


def sh(cmd, cwd, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode != 0:
        raise AssertionError("%s failed: %s" % (cmd, p.stdout))
    return p.stdout


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def slurp(path):
    with open(path) as f:
        return f.read()


def load(path):
    return json.loads(slurp(path))


# Settings fx init writes in each repository entry of factory.json (a value there replaces the top level's).
REPO_KEYS = ("setup", "app", "e2e", "copy_into_worktree", "protected_paths")

class Box:
    """One machine, as far as fx can tell: its own HOME, state, tools and PATH. Every fx is a separate process."""

    def __init__(self, root):
        self.root = root
        self.bin = os.path.join(root, "bin")
        os.makedirs(self.bin)
        for tool in ("gitleaks", "semgrep", "osv-scanner", "docker"):
            p = os.path.join(self.bin, tool)
            write(p, STUB % {"python": sys.executable, "secret_names": sorted(SECRET_ENV)})
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for real in ("git", "bash", "tar", "sleep", "env", "grep", "test"):
            w = shutil.which(real)
            if w and not os.path.exists(os.path.join(self.bin, real)):
                os.symlink(w, os.path.join(self.bin, real))
        gitcfg = os.path.join(root, "gitconfig")
        write(gitcfg, "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        self.out = os.path.join(root, "out")
        os.makedirs(self.out)
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith(("JIRA_", "GH_", "GITHUB_", "GITLAB_", "GLAB_", "CLAUDE", "ANTHROPIC", "FACTORY_"))}
        keep.update(HOME=root, GIT_CONFIG_GLOBAL=gitcfg, GIT_CONFIG_NOSYSTEM="1",
                    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                    FACTORY_STATE_DIR=os.path.join(root, "state"), FACTORY_TOOLS_DIR=os.path.join(root, "tools"),
                    FACTORY_WORKSPACE=os.path.join(root, "machine", "workspace.json"), FACTORY_SAMPLE_SEC="0",
                    PATH=self.bin + os.pathsep + "/usr/bin:/bin", STUB_LOG=os.path.join(root, "stub.log"),
                    FX_TEST_OUT=self.out, TMPDIR=os.path.join(root, "tmp"),
                    FACTORY_FAKE_MACHINE=os.path.join(root, "machine.json"), FACTORY_POLL_SEC="0.1")
        os.makedirs(keep["TMPDIR"])
        self.env = keep
        self.n = 0
        self.sleepers = []
        self.set_machine(cpus=256, host={"total_mb": 4096000})  # big and idle, whatever machine runs the tests

    def set_machine(self, **m):
        m.setdefault("listening", [])  # which ports are free must not depend on what runs on the machine the tests run on
        write(self.env["FACTORY_FAKE_MACHINE"] + ".tmp", json.dumps(m))
        os.replace(self.env["FACTORY_FAKE_MACHINE"] + ".tmp", self.env["FACTORY_FAKE_MACHINE"])

    def ledger(self):
        try:
            return load(os.path.join(self.env["FACTORY_STATE_DIR"], "run", "state.json"))
        except (OSError, ValueError):
            return {"leases": {}, "queue": [], "ports": {}}

    def session(self, name):
        """The environment of one chat session: its id, and a process standing for its window (kill it to close it)."""
        p = subprocess.Popen(["sleep", "600"], env=self.env)
        self.sleepers.append(p)
        return dict(self.env, CLAUDE_CODE_SESSION_ID=(name * 36)[:36], CLAUDE_PID=str(p.pid)), p

    def close(self):
        for p in self.sleepers:
            if p.poll() is None:
                p.kill()
                p.wait()

    def popen(self, factory, *argv, stdin=None, env=None):
        return subprocess.Popen([sys.executable, FX_PATH, "--repo", factory] + list(argv), cwd=self.root, env=env or self.env,
                                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    @staticmethod
    def result(p, stdin=None, timeout=300):
        out, err = p.communicate(stdin, timeout=timeout)
        try:
            res = json.loads(out)
        except ValueError:
            res = {"unparsed": out[-600:], "stderr": err[-600:]}
        return p.returncode, res

    def fx(self, factory, *argv, stdin=None, env=None):
        return self.result(self.popen(factory, *argv, stdin=stdin, env=env), stdin)

    def project(self, name):
        seed = os.path.join(self.root, "seed-" + name)
        os.makedirs(seed)
        sh(["git", "init", "-q", "-b", "main"], seed, self.env)
        write(os.path.join(seed, "README.md"), "%s\n" % name)
        write(os.path.join(seed, "src", "a.py"), "a = 1\n")
        write(os.path.join(seed, "src", "b.py"), "b = 1\n")
        write(os.path.join(seed, "web", "c.js"), "var c = 1;\n")
        sh(["git", "add", "-A"], seed, self.env)
        sh(["git", "commit", "-q", "-m", "base"], seed, self.env)
        origin = os.path.join(self.root, name + ".git")
        sh(["git", "clone", "-q", "--bare", seed, origin], self.root, self.env)
        return origin

    def factory(self, name, origin=None, checks="#!/usr/bin/env bash\nexit 0\n", **conf_changes):
        origin = origin or self.project(name)
        factory = os.path.join(self.root, name + "-factory")
        os.makedirs(factory)
        sh(["git", "init", "-q", "-b", "main"], factory, self.env)
        code, res = self.fx(factory, "init", "--project", origin)
        if code != 0:
            raise AssertionError("fx init failed: %s" % res)
        path = os.path.join(factory, "factory.json")
        conf = load(path)
        conf["project"]["name"] = name
        conf["jira"] = {"base_url": JIRA, "transitions": {"start": "In Progress", "review": "In Review"}}
        conf["security"] = {"required": [], "scanners": {"zap": False, "headers": False}}
        conf["learning"] = {"ask_after": "never"}
        conf["ports"] = {"base": PORT_BASE, "step": 10}
        for key in REPO_KEYS:  # a repository's own settings (init writes them in its entry, where they win)
            if key in conf_changes and conf.get("repos"):
                conf["repos"][0][key] = conf_changes.pop(key)
        conf.update(conf_changes)
        write(path, json.dumps(conf, indent=1))
        write(self.checks_script(factory), checks)
        kdir = os.path.join(factory, "knowledge")
        for fname in os.listdir(kdir):
            p = os.path.join(kdir, fname)
            if os.path.isfile(p):
                write(p, slurp(p).replace("FACTORY_TODO", "done"))
        write(os.path.join(factory, ".gitignore"), GITIGNORE)
        sh(["git", "add", "-A"], factory, self.env)
        sh(["git", "commit", "-q", "-m", "factory"], factory, self.env)
        return factory

    @staticmethod
    def checks_script(factory):
        """The checks script of the factory's repository: checks/<repo>.sh (checks.sh for a factory without repos)."""
        repos = load(os.path.join(factory, "factory.json")).get("repos")
        return os.path.join(factory, "checks", "%s.sh" % repos[0]["name"]) if repos else os.path.join(factory, "checks.sh")

    def issue_file(self, factory, key, summary="Do the thing"):
        self.n += 1
        path = os.path.join(factory, "jobs", ".incoming", "%s-%d.md" % (key, self.n))
        write(path, "# %s: %s\n\n- Link: %s/browse/%s\n\n## Description\nSomething to build.\n" % (key, summary, JIRA, key))
        return path

    def new_args(self, factory, key):
        return ["new", key, "--issue-file", self.issue_file(factory, key), "--base", "main"]

    def new(self, factory, key, env=None):
        code, res = self.fx(factory, *self.new_args(factory, key), env=env)
        if code != 0:
            raise AssertionError("fx new %s failed: %s" % (key, res))
        return res

    @staticmethod
    def job_path(factory, job):
        return os.path.join(factory, "jobs", job, "job.json")

    def job(self, factory, job):
        """job.json with its first changed repository's fields at the top as well, as fx shows it (edit writes them
        back at the top, where fx takes them for that repository's)."""
        from test_security import fx
        return fx.job_flat_copy(load(self.job_path(factory, job)))

    def edit(self, factory, job, **changes):
        j = self.job(factory, job)
        j.update(changes)
        write(self.job_path(factory, job), json.dumps(j, indent=1))
        return j

    def to_build(self, factory, job, files=None):
        """As after intake, spec and a build: a commit on the job's branch and the job in stage build."""
        j = self.edit(factory, job, stage="build", risk="high", setup_done=True, reviewers=["security", "code"],
                      models={"security": "opus", "code": "sonnet", "builder": "opus", "ux": "sonnet", "ui": "sonnet"})
        for rel, text in (files or {"src/new_%s.py" % job.lower().replace("-", "_"): "x = 1\n"}).items():
            write(os.path.join(j["worktree"], rel), text)
        sh(["git", "add", "-A"], j["worktree"], self.env)
        sh(["git", "commit", "-q", "-m", "%s: change" % job], j["worktree"], self.env)
        return j


def alive(pid):
    """Still running. Not one that died and only waits for its parent to notice: in a container nobody may."""
    return fx.proc_starts([pid]).get(pid) is not None


class BoxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-par-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.box = Box(self.tmp)
        self.addCleanup(self.box.close)

    def wait_for(self, cond, timeout=20.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def gate_running(self, factory, job):
        return lambda: any(s.get("step") == "gate" and not s.get("ended") for s in self.box.job(factory, job).get("steps") or [])


# --------------------------------------------------------------------------
# 1. nothing is handed out twice

class PortsTest(BoxCase):
    def test_sessions_that_start_jobs_at_the_same_moment_get_their_own_ports(self):
        f = self.box.factory("race")
        k = 0
        for _round in range(3):
            keys = ["RACE-%d" % (k + i + 1) for i in range(6)]
            k += 6
            procs = [self.box.popen(f, *self.box.new_args(f, key)) for key in keys]
            results = [self.box.result(p) for p in procs]
            self.assertEqual([c for c, _ in results], [0] * 6, results)
            ports = [self.box.job(f, key)["port"] for key in keys]
            self.assertEqual(len(set(ports)), 6, ports)
            self.assertTrue(all(p > PORT_BASE and (p - PORT_BASE) % 10 == 0 for p in ports), ports)
            for key in keys:  # these jobs end, so their ports are free for the next round
                self.box.edit(f, key, stage="rejected")
        self.assertEqual(sorted(ports), [PORT_BASE + 10 * i for i in range(1, 7)])  # and were taken again

    def test_two_projects_on_one_machine_do_not_share_ports(self):
        a, b = self.box.factory("pa"), self.box.factory("pb")
        ja, jb = self.box.new(a, "AAA-1"), self.box.new(b, "BBB-1")
        self.assertNotEqual(ja["port"], jb["port"])
        self.assertEqual({ja["port"], jb["port"]}, {PORT_BASE + 10, PORT_BASE + 20})

    def test_a_job_made_by_an_older_fx_keeps_its_ports(self):
        """job.json is what says who holds which ports, so a job the ledger never heard of counts all the same."""
        a, b = self.box.factory("oa"), self.box.factory("ob")
        self.box.new(a, "OLD-1")
        shutil.rmtree(os.path.join(self.tmp, "state", "run"))  # as if that fx never kept a ledger
        self.assertEqual(self.box.new(b, "NEW-1")["port"], PORT_BASE + 20)

    def test_a_port_something_else_listens_on_is_passed_over(self):
        f = self.box.factory("busy")
        self.box.set_machine(cpus=256, host={"total_mb": 4096000}, listening=[PORT_BASE + 12])  # inside the first block
        self.assertEqual(self.box.new(f, "LSN-1")["port"], PORT_BASE + 20)

    def test_what_listens_is_asked_of_the_machine_itself(self):
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("FACTORY_FAKE_MACHINE", None)  # a real machine: nothing says for it what listens
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                s.listen(1)
                port = s.getsockname()[1]
                self.assertTrue(fx.port_listening(port))
            self.assertFalse(fx.port_listening(port))

    def test_a_review_sandbox_and_a_new_job_never_get_the_same_port(self):
        """The sandbox's app answers minutes after its port was chosen: until then only the ledger knows it is taken."""
        f = self.box.factory("rev")
        cwd, old = os.getcwd(), dict(os.environ)
        os.environ.clear()
        os.environ.update(self.box.env)
        os.chdir(f)
        try:
            ctx = fx.Ctx()
            p1 = fx.review_free_port(ctx, {"id": "_reviews/RV-1-mr1"})
            p2 = fx.review_free_port(ctx, {"id": "_reviews/RV-2-mr2"})
            job = self.box.new(f, "JOB-1")  # another process, while this one still holds both
        finally:
            fx.ports_done()
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(len({p1, p2, job["port"]}), 3, (p1, p2, job["port"]))

    def test_each_package_of_a_split_build_gets_a_block_of_its_own(self):
        f = self.box.factory("split")
        job = self.box.new(f, "SPL-1")["job"]
        self.box.edit(f, job, stage="build")
        write(os.path.join(f, "jobs", job, "plan.md"), PLAN)
        code, res = self.box.fx(f, "split", job)
        self.assertEqual(code, 0, res)
        j = self.box.job(f, job)
        ports = sorted([j["port"]] + [p["port"] for p in j["split"]["packages"].values()])
        self.assertEqual(ports, [PORT_BASE + 10 * i for i in range(1, 5)])  # the project's stack may use base, +1, +2
        code, res = self.box.fx(f, "split", job, "--update")  # the planner changed the plan: the ports stay
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted([j["port"]] + [p["port"] for p in self.box.job(f, job)["split"]["packages"].values()]), ports)
        other = self.box.new(f, "SPL-2")  # and a job that starts meanwhile is not given a package's
        self.assertEqual(other["port"], PORT_BASE + 50)

    def test_a_rework_takes_its_old_ports_again_when_they_are_free(self):
        f = self.box.factory("rework")
        job = self.box.new(f, "RWK-1")["job"]
        port = self.box.job(f, job)["port"]
        self.box.edit(f, job, stage="handed-off")
        code, res = self.box.fx(f, "rework", job, "change it")
        self.assertEqual((code, res.get("port")), (0, port), res)

    def test_a_rework_moves_when_another_job_was_given_its_ports(self):
        """A handed-off job holds no ports, so the next job may be given the same ones: what older versions left on a
        machine looks like this too. Sent back for rework, the first job takes other ports and the second keeps its own."""
        f = self.box.factory("moved")
        done = self.box.new(f, "MOV-1")["job"]
        port = self.box.job(f, done)["port"]
        self.box.edit(f, done, stage="handed-off")
        busy = self.box.new(f, "MOV-2")["job"]
        self.assertEqual(self.box.job(f, busy)["port"], port)  # free: the first job is over
        self.box.edit(f, busy, stage="build")
        code, res = self.box.fx(f, "rework", done, "change it")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["port"], port + 10)
        self.assertTrue(any("ports moved" in n for n in res["notes"]), res)
        self.assertEqual((self.box.job(f, done)["port"], self.box.job(f, busy)["port"]), (port + 10, port))
        self.assertEqual(self.box.new(f, "MOV-3")["port"], port + 20)  # and both count for the job after them


class NewJobTest(BoxCase):
    def test_local_tasks_started_at_the_same_moment_each_get_a_number(self):
        f = self.box.factory("local")
        procs = []
        for i in range(5):
            path = os.path.join(f, "jobs", ".incoming", "task-%d.md" % i)
            write(path, "Build thing %d.\n" % i)
            procs.append(self.box.popen(f, "new", "--local", "Task %d" % i, "--issue-file", path, "--base", "main"))
        results = [self.box.result(p) for p in procs]
        self.assertEqual([c for c, _ in results], [0] * 5, results)
        self.assertEqual(sorted(r["job"] for _, r in results), ["LOCAL-%d" % n for n in range(1, 6)])
        self.assertEqual(len({r["port"] for _, r in results}), 5)

    def test_a_job_being_made_is_not_taken_for_one_that_stopped_halfway(self):
        f = self.box.factory("making")
        setup = os.path.join(self.tmp, "slow-git")  # the first session is still cloning when the second asks
        os.makedirs(os.path.join(f, "jobs", "MAK-1"))
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys, time; sys.path.insert(0, %r); from test_security import fx; import os; os.chdir(%r)\n"
            "ctx = fx.Ctx(); claim = {}\n"
            "f = open(fx.lock_file_for('creating', ctx.job_dir('MAK-1')), 'a+'); fx.flock_wait(f, 'exclusive', 0)\n"
            "open(%r, 'w').close(); time.sleep(30)") % (os.path.dirname(os.path.abspath(__file__)), f, setup)], env=self.box.env)
        self.addCleanup(holder.kill)
        self.wait_for(lambda: os.path.exists(setup), what="the first session to hold the job")
        code, res = self.box.fx(f, *self.box.new_args(f, "MAK-1"))
        self.assertEqual(code, 2, res)
        self.assertIn("another session is making", res["error"])
        self.assertNotIn("--again", res["error"])
        holder.kill()
        holder.wait()
        code, res = self.box.fx(f, *self.box.new_args(f, "MAK-1"))  # nobody is at it any more: now it did stop halfway
        self.assertEqual(code, 2, res)
        self.assertIn("stopped halfway", res["error"])

    def test_a_new_job_that_fails_leaves_no_folder_behind(self):
        f = self.box.factory("fails")
        code, res = self.box.fx(f, "new", "FAIL-1", "--issue-file", self.box.issue_file(f, "FAIL-1"), "--base", "no-such-branch")
        self.assertEqual(code, 3, res)
        self.assertFalse(os.path.exists(os.path.join(f, "jobs", "FAIL-1")))

    def test_a_branch_of_the_issue_already_on_the_remote_is_said(self):
        f = self.box.factory("twice")
        origin = load(os.path.join(f, "factory.json"))["repos"][0]["url"]
        sh(["git", "branch", "factory/TWO-1-from-another-machine", "main"], origin, self.box.env)
        res = self.box.new(f, "TWO-1")
        self.assertTrue(any("factory/TWO-1-from-another-machine" in n for n in res["notes"]), res)
        self.assertNotIn("notes", self.box.new(f, "TWO-2"))


class SetupEnvTest(BoxCase):
    NAMES = "FACTORY_JOB|FACTORY_PORT|FACTORY_DB"

    def test_setup_is_told_the_jobs_port_and_name_like_the_checks(self):
        setup = "env | grep -E '^(%s)=' > \"$FX_TEST_OUT/setup.env\"" % self.NAMES
        checks = "#!/usr/bin/env bash\nenv | grep -E '^(%s)=' > \"$FX_TEST_OUT/checks.env\"\nexit 0\n" % self.NAMES
        f = self.box.factory("env", checks=checks, setup=setup)
        job = self.box.new(f, "ENV-1")["job"]
        code, res = self.box.fx(f, "setup", job)
        self.assertEqual(code, 0, res)
        self.box.to_build(f, job)
        code, res = self.box.fx(f, "gate", job)
        self.assertEqual(res.get("gate"), "PASS", res)
        told = dict(l.split("=", 1) for l in slurp(os.path.join(self.box.out, "setup.env")).split())
        self.assertEqual(told, dict(l.split("=", 1) for l in slurp(os.path.join(self.box.out, "checks.env")).split()))
        self.assertEqual(told["FACTORY_JOB"], "ENV-1")
        self.assertEqual(told["FACTORY_PORT"], str(self.box.job(f, job)["port"]))

    def test_each_candidate_is_set_up_with_its_own_port_and_name(self):
        setup = "env | grep -E '^(%s)=' > \"$FX_TEST_OUT/setup-$FACTORY_JOB.env\"" % self.NAMES
        f = self.box.factory("cand", setup=setup)
        job = self.box.new(f, "CAN-1")["job"]
        self.box.edit(f, job, stage="build", best_of=2)
        code, res = self.box.fx(f, "candidates", job)
        self.assertEqual(code, 0, res)
        j = self.box.job(f, job)
        ports = [c["port"] for c in j["candidates"]]
        self.assertEqual(sorted(ports + [j["port"]]), [PORT_BASE + 10, PORT_BASE + 20, PORT_BASE + 30])
        for c in j["candidates"]:
            told = dict(l.split("=", 1) for l in slurp(os.path.join(self.box.out, "setup-CAN-1-c%d.env" % c["k"])).split())
            self.assertEqual(told["FACTORY_PORT"], str(c["port"]))
            self.assertEqual(told["FACTORY_DB"], "factory_can_1_c%d" % c["k"])


# --------------------------------------------------------------------------
# 2. no command wipes what another wrote, and one job runs one long step at a time

class OneJobSeveralCommandsTest(BoxCase):
    def test_what_is_noted_during_a_gate_is_still_there_after_it(self):
        f = self.box.factory("upd", checks=SLOW_CHECKS % 3)
        job = self.box.new(f, "UPD-1")["job"]
        self.box.to_build(f, job)
        gate = self.box.popen(f, "gate", job)
        self.wait_for(self.gate_running(f, job), what="the gate to start")
        code, res = self.box.fx(f, "agent-id", job, "agent-123", "--model", "opus")
        self.assertEqual(code, 0, res)
        gcode, gres = self.box.result(gate)
        self.assertEqual(gres.get("gate"), "PASS", gres)
        j = self.box.job(f, job)
        self.assertEqual((j["builder_agent_id"], j["stage"], j["round"]), ("agent-123", "review", 1))
        notes = [h["note"] for h in j["history"]]
        self.assertTrue(any("builder agent agent-123" in n for n in notes) and any("gate PASS" in n for n in notes), notes)

    def test_four_prompts_asked_in_one_message_are_all_recorded(self):
        f = self.box.factory("ctx")
        job = self.box.new(f, "CTX-1")["job"]
        self.box.to_build(f, job)
        self.box.edit(f, job, stage="review", round=1, reviewers=["security", "code", "ux", "ui"])
        for _round in range(4):
            procs = [self.box.popen(f, "ctx", job, "--agent", a) for a in ("security", "code", "ux", "ui")]
            self.assertEqual([self.box.result(p)[0] for p in procs], [0] * 4)
            runs = [r["agent"] for r in self.box.job(f, job)["runs"] if not r.get("ended")]
            self.assertEqual(sorted(runs), ["code", "security", "ui", "ux"])
            for a in ("security", "code", "ux", "ui"):
                code, res = self.box.fx(f, "usage", job, "--agent", a, "--tokens", "10")
                self.assertEqual(code, 0, res)

    def test_prompts_asked_at_once_are_all_recorded_when_the_list_is_at_its_length(self):
        """job.json keeps the latest 300 runs. Cut before two commands' additions are put together, the older ones move
        up under one of them and its run lands on the other's."""
        f = self.box.factory("full")
        job = self.box.new(f, "FUL-1")["job"]
        self.box.to_build(f, job)
        old = [{"agent": "code", "name": "code", "wp": None, "cand": None, "round": 0, "model": "sonnet", "status": "done",
                "started": "2026-01-01T%02d:%02d:%02d+00:00" % (i // 3600, i // 60 % 60, i % 60),
                "ended": "2026-01-02T00:00:00+00:00", "result": []} for i in range(300)]
        self.box.edit(f, job, stage="review", round=1, reviewers=["security", "code", "ux", "ui"], runs=old)
        for _round in range(3):
            procs = [self.box.popen(f, "ctx", job, "--agent", a) for a in ("security", "code", "ux", "ui")]
            self.assertEqual([self.box.result(p)[0] for p in procs], [0] * 4)
            runs = self.box.job(f, job)["runs"]
            self.assertEqual(sorted(r["agent"] for r in runs if not r.get("ended")), ["code", "security", "ui", "ux"])
            self.assertEqual(len(runs), 300)
            for a in ("security", "code", "ux", "ui"):
                self.assertEqual(self.box.fx(f, "usage", job, "--agent", a, "--tokens", "10")[0], 0)

    def test_what_a_killed_gate_left_running_is_stopped_before_the_next_one_starts(self):
        """Killed outright (no memory left, kill -9), fx cannot stop the checks it started: they would go on in the clone
        beside the checks of the gate that is run again."""
        checks = ('#!/usr/bin/env bash\necho $$ > "$FX_TEST_OUT/checks-pid"\n'
                  'if [ ! -e "$FX_TEST_OUT/second" ]; then sleep 60; fi\nexit 0\n')
        f = self.box.factory("orphan", checks=checks)
        job = self.box.new(f, "ORP-1")["job"]
        self.box.to_build(f, job)
        gate = self.box.popen(f, "gate", job)
        pidfile = os.path.join(self.box.out, "checks-pid")
        self.wait_for(lambda: os.path.exists(pidfile) and slurp(pidfile).strip(), what="the checks to run")
        left = int(slurp(pidfile))
        self.addCleanup(lambda: alive(left) and os.kill(left, signal.SIGKILL))
        gate.kill()
        gate.communicate()
        self.assertTrue(alive(left))  # the checks of the killed gate are still running
        write(os.path.join(self.box.out, "second"), "")
        code, res = self.box.fx(f, "gate", job)
        self.assertEqual(res.get("gate"), "PASS", res)
        self.assertFalse(alive(left))
        self.assertNotEqual(int(slurp(pidfile)), left)  # and the second gate ran its own
        gates = [(s.get("result"), bool(s.get("ended"))) for s in self.box.job(f, job)["steps"] if s["step"] == "gate"]
        self.assertEqual(gates, [("stopped", True), ("PASS", True)])  # the board does not show the killed one running

    def test_a_helper_the_set_up_leaves_running_on_purpose_is_left_alone(self):
        """Only what a killed fx left is stopped. A command that ended and left something running did so itself."""
        f = self.box.factory("helper", setup='sleep 60 >/dev/null 2>&1 & echo $! > "$FX_TEST_OUT/helper-pid"')
        job = self.box.new(f, "HLP-1")["job"]
        code, res = self.box.fx(f, "setup", job)
        self.assertEqual(code, 0, res)
        helper = int(slurp(os.path.join(self.box.out, "helper-pid")))
        self.addCleanup(lambda: alive(helper) and os.kill(helper, signal.SIGKILL))
        self.box.to_build(f, job)
        self.assertEqual(self.box.fx(f, "gate", job)[1].get("gate"), "PASS")
        self.assertTrue(alive(helper))

    def test_a_second_gate_of_the_same_job_is_told_to_wait(self):
        f = self.box.factory("twice", checks=SLOW_CHECKS % 3)
        job = self.box.new(f, "TWI-1")["job"]
        self.box.to_build(f, job)
        first = self.box.popen(f, "gate", job)
        self.wait_for(self.gate_running(f, job), what="the first gate to start")
        code, res = self.box.fx(f, "gate", job)
        self.assertEqual(code, 75, res)
        self.assertEqual((res["next"], res["busy"]["step"]), ("wait", "gate"))
        self.assertEqual(self.box.result(first)[1].get("gate"), "PASS")
        j = self.box.job(f, job)
        self.assertEqual((j["round"], len([s for s in j["steps"] if s["step"] == "gate"])), (1, 1))
        self.assertEqual(sorted(d for d in os.listdir(os.path.join(f, "jobs", job)) if d.startswith("round-")), ["round-1"])


class SettingsChangeUnderAGateTest(BoxCase):
    def test_a_commit_for_another_job_costs_this_one_no_round(self):
        f = self.box.factory("other", checks="#!/usr/bin/env bash\ntouch \"$FX_TEST_OUT/checks-run\"\nsleep 3\nexit 0\n")
        job = self.box.new(f, "GATE-1")["job"]
        self.box.to_build(f, job)
        write(os.path.join(f, "jobs", job, "build.md"), "STATUS: READY\n")
        gate = self.box.popen(f, "gate", job)
        self.wait_for(lambda: os.path.exists(os.path.join(self.box.out, "checks-run")), what="the gate's checks to run")
        path = os.path.join(f, "factory.json")
        conf = load(path)
        conf["repos"][0]["protected_paths"] = ["tests/unlocked-for-another-job.txt"]
        write(path, json.dumps(conf, indent=1))
        sh(["git", "commit", "-q", "-am", "Unlock a test for OTHER-9 (user approved)"], f, self.box.env)
        code, res = self.box.result(gate)
        self.assertEqual(code, 3, res)
        self.assertTrue(res["void"], res)
        self.assertEqual(res["next"], "gate")
        j = self.box.job(f, job)
        self.assertEqual((j["round"], j["stage"], j.get("gate"), j["void"]["count"]), (0, "build", None, 1))
        jd = os.path.join(f, "jobs", job)
        self.assertTrue(os.path.exists(os.path.join(jd, "build.md")))  # the builder's report is back where the gate reads it
        self.assertEqual(sorted(d for d in os.listdir(jd) if d.startswith("round-")), ["round-1-void-1"])
        self.assertEqual([s.get("result") for s in j["steps"] if s["step"] == "gate"], ["void"])
        code, res = self.box.fx(f, "gate", job)  # run again, as it says: the same round, and this time it counts
        self.assertEqual((res.get("gate"), res.get("round"), res.get("rounds_left")), ("PASS", 1, 2), res)
        self.assertNotIn("void", self.box.job(f, job))

    def test_another_program_on_the_jobs_port_costs_it_no_round(self):
        f = self.box.factory("port", app={"up": "sleep 30", "start_timeout_sec": 5})
        job = self.box.new(f, "PRT-1")["job"]
        self.box.to_build(f, job)
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            self.box.edit(f, job, port=other.getsockname()[1])
            code, res = self.box.fx(f, "gate", job)
        self.assertEqual(code, 3, res)
        self.assertIn("already answers", res["error"])
        self.assertTrue(res["void"])
        self.assertEqual(self.box.job(f, job)["round"], 0)


class OwnClonesTest(BoxCase):
    def test_each_session_reads_its_own_clone(self):
        f = self.box.factory("read")
        a, _ = self.box.session("a")
        b, _ = self.box.session("b")
        pa = self.box.fx(f, "code", "open", env=a)[1]["path"]
        write(os.path.join(pa, "session-a-is-reading.txt"), "x")
        pb = self.box.fx(f, "code", "open", env=b)[1]["path"]
        self.assertNotEqual(pa, pb)
        self.box.fx(f, "code", "close", env=b)
        self.assertTrue(os.path.exists(os.path.join(pa, "session-a-is-reading.txt")))
        self.assertFalse(os.path.exists(pb))
        self.assertEqual(self.box.fx(f, "where", env=a)[1]["code"], pa)

    def test_two_selftests_take_turns(self):
        f = self.box.factory("self", checks="#!/usr/bin/env bash\necho start >> \"$FX_TEST_OUT/selftests\"\nsleep 2\n"
                                              "test -f README.md && echo end >> \"$FX_TEST_OUT/selftests\"\n")
        first = self.box.popen(f, "selftest", "--no-app")
        self.wait_for(lambda: os.path.exists(os.path.join(self.box.out, "selftests")), what="the first selftest's checks")
        second = self.box.popen(f, "selftest", "--no-app")
        (c1, r1), (c2, r2) = self.box.result(first), self.box.result(second)
        self.assertEqual((c1, c2), (0, 0), (r1, r2))
        self.assertTrue(r1["ok"] and r2["ok"], (r1.get("steps"), r2.get("steps")))
        # one after the other, and the second one's clone was not deleted under it
        self.assertEqual(slurp(os.path.join(self.box.out, "selftests")).split(), ["start", "end", "start", "end"])


class SessionsTest(BoxCase):
    def test_a_job_another_open_session_is_at_has_to_be_taken_over(self):
        f = self.box.factory("own")
        a, window_a = self.box.session("a")
        b, _ = self.box.session("b")
        job = self.box.new(f, "OWN-1", env=a)["job"]
        self.assertEqual(self.box.job(f, job)["held_by"]["session"], a["CLAUDE_CODE_SESSION_ID"])
        code, res = self.box.fx(f, "ctx", job, "--agent", "intake", env=b)
        self.assertEqual((code, res.get("next")), (2, "ask-take"), res)
        self.assertIn("take %s" % job, res["then"])
        self.assertEqual(self.box.fx(f, "show", job, env=b)[0], 0)  # looking is always fine
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=a)[0], 0)  # and its own session goes on
        code, res = self.box.fx(f, "take", job, env=b)
        self.assertEqual(code, 0, res)
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=b)[0], 0)
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=a)[0], 2)  # now it is the other way round

    def test_a_job_that_was_taken_over_stays_taken(self):
        """The session it was taken from is still open, and a command of its own that only notes something (what an
        agent used, after it finished) saves the job. That must not take the job back."""
        f = self.box.factory("kept")
        a, _ = self.box.session("a")
        b, _ = self.box.session("b")
        job = self.box.new(f, "KPT-1", env=a)["job"]
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=a)[0], 0)
        self.assertEqual(self.box.fx(f, "take", job, env=b)[0], 0)
        code, res = self.box.fx(f, "usage", job, "--agent", "intake", "--tokens", "10", env=a)
        self.assertEqual(code, 0, res)
        self.assertEqual(self.box.job(f, job)["held_by"]["session"], b["CLAUDE_CODE_SESSION_ID"])
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=a)[0], 2)
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake", env=b)[0], 0)

    def test_a_job_of_a_session_that_was_closed_is_simply_picked_up(self):
        f = self.box.factory("closed")
        a, window_a = self.box.session("a")
        b, _ = self.box.session("b")
        job = self.box.new(f, "CLS-1", env=a)["job"]
        window_a.kill()
        window_a.wait()
        code, res = self.box.fx(f, "ctx", job, "--agent", "intake", env=b)
        self.assertEqual(code, 0, res)
        self.assertEqual(self.box.job(f, job)["held_by"]["session"], b["CLAUDE_CODE_SESSION_ID"])

    def test_outside_a_session_nothing_is_refused(self):
        f = self.box.factory("term")
        a, _ = self.box.session("a")
        job = self.box.new(f, "TRM-1", env=a)["job"]
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "intake")[0], 0)  # a terminal: fx cannot tell, so it does not judge


# --------------------------------------------------------------------------
# 3. a split build opens only as many packages as the machine has room for, and stops the stacks it no longer needs

FOUR = """PLAN: SPLIT

```json
{"packages": [
  {"id": "wp0", "title": "a", "owns": ["src/a.py"]},
  {"id": "wp1", "title": "b", "owns": ["src/b.py"]},
  {"id": "wp2", "title": "c", "owns": ["web/*"]},
  {"id": "wp3", "title": "d", "owns": ["README.md"]}
]}
```
"""


class PackagesTest(BoxCase):
    def split(self, name="pk", key="SPL-1", f=None, **conf):
        f = f or self.box.factory(name, **conf)
        job = self.box.new(f, key)["job"]
        self.box.edit(f, job, stage="build", setup_done=True)
        write(os.path.join(f, "jobs", job, "plan.md"), FOUR)
        code, res = self.box.fx(f, "split", job)
        self.assertEqual(code, 0, res)
        return f, job

    def finish_package(self, f, job, pid, rel):
        clone = self.box.job(f, job)["split"]["packages"][pid]["clone"]
        write(os.path.join(clone, rel), "changed by %s\n" % pid)
        sh(["git", "add", "-A"], clone, self.box.env)
        sh(["git", "commit", "-q", "-m", pid], clone, self.box.env)
        write(os.path.join(f, "jobs", job, "build-%s.md" % pid), "STATUS: READY\n")
        code, res = self.box.fx(f, "wp", "done", job, pid)
        self.assertEqual((code, res.get("status")), (0, "integrated"), res)

    def places(self):
        return sorted(l["name"] for l in self.box.ledger()["leases"].values() if l["kind"] == "package")

    def test_on_a_roomy_machine_every_ready_package_starts(self):
        f, job = self.split()
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual([p["id"] for p in res["start"]], ["wp0", "wp1", "wp2", "wp3"], res)
        self.assertNotIn("waiting_for_room", res)

    def test_only_as_many_packages_start_as_the_machine_has_room_for(self):
        """A package not measured yet counts as a tenth of the machine's memory and a quarter of its CPUs."""
        f, job = self.split()
        self.box.set_machine(cpus=8, host={"total_mb": 16000, "other_mb": 10000})  # 13600 to give, 10000 of it in use
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual(([p["id"] for p in res["start"]], res["waiting_for_room"]), (["wp0", "wp1"], ["wp2", "wp3"]), res)
        self.assertIn("the machine's memory", res["why"])
        self.assertEqual(self.places(), ["wp0", "wp1"])
        code, res = self.box.fx(f, "wp", "next", job)  # asked again while they build: the answer is the same
        self.assertEqual((res["next"], res["building"], res["waiting_for_room"]), ("wait", ["wp0", "wp1"], ["wp2", "wp3"]), res)
        self.finish_package(f, job, "wp0", "src/a.py")  # one is merged: its room is free
        self.assertEqual(self.places(), ["wp1"])
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual(([p["id"] for p in res["start"]], res["waiting_for_room"]), (["wp2"], ["wp3"]), res)

    def test_with_no_room_at_all_the_job_waits_for_it_and_is_called_when_there_is(self):
        f, job = self.split()
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        other = subprocess.Popen([sys.executable, FX_PATH, "run", "--name", "another job's gate", "--need", "host_mb=13000",
                                  "--", "sleep", "4"], env=self.box.env, cwd=self.tmp, stderr=subprocess.DEVNULL)
        self.addCleanup(other.kill)
        self.wait_for(lambda: self.box.ledger()["leases"], what="the other step to hold its place")
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual((code, res["next"], res["waiting_for_room"]), (0, "wait-room", ["wp0", "wp1", "wp2", "wp3"]), res)
        self.assertIn("wp next %s --wait" % job, res["then"])
        t0 = time.time()
        code, res = self.box.fx(f, "wp", "next", job, "--wait")  # what the orchestrator runs in the background
        self.assertEqual(code, 0, res)
        self.assertEqual([p["id"] for p in res["start"]][:1], ["wp0"], res)
        self.assertEqual(other.wait(timeout=30), 0)  # it came back because the other step had ended, not before
        self.assertGreater(time.time() - t0, 1.0)

    def test_a_machine_can_be_told_how_many_packages_it_builds_at_once(self):
        f, job = self.split()
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"max_builders": 1}))
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual(([p["id"] for p in res["start"]], res["waiting_for_room"]), (["wp0"], ["wp1", "wp2", "wp3"]), res)
        self.assertIn("at most 1", res["why"])

    def test_a_package_waits_for_its_turn_under_that_limit_like_for_room(self):
        """`fx wp next --wait` is what a session runs in the background when it was told `wait-room`: it must wait there,
        not come back at once to be run again and again."""
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"max_builders": 1}))
        f, first = self.split(key="AAA-1")
        _, second = self.split(key="BBB-1", f=f)
        self.assertEqual([p["id"] for p in self.box.fx(f, "wp", "next", first)[1]["start"]], ["wp0"])
        code, res = self.box.fx(f, "wp", "next", second)
        self.assertEqual((res["next"], res["waiting_for_room"][:1]), ("wait-room", ["wp0"]), res)
        self.assertIn("at most 1", res["why"])
        waiter = self.box.popen(f, "wp", "next", second, "--wait")
        self.addCleanup(lambda: (waiter.kill(), waiter.communicate()))
        time.sleep(1.5)
        self.assertIsNone(waiter.poll())  # still waiting: the other job's package is being built
        self.finish_package(f, first, "wp0", "src/a.py")
        code, res = self.box.result(waiter, timeout=60)
        self.assertEqual((code, [p["id"] for p in res.get("start", [])]), (0, ["wp0"]), res)

    def test_a_package_not_measured_yet_holds_no_share_of_dockers_memory(self):
        """Its builder's work is processes on the machine, and its stack counts at what it is measured to use once it is
        up. With a quarter of Docker's memory held for each as well, a machine that other programs already fill opened one
        package at a time, and nothing a builder did ever changed that."""
        f, job = self.split()
        self.box.set_machine(cpus=10, host={"total_mb": 24576, "other_mb": 8344},
                             docker={"total_mb": 16110, "other_mb": 6402, "cpus": 10})  # 6144 MB of the machine to give
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual(([p["id"] for p in res["start"]], res["waiting_for_room"]), (["wp0", "wp1"], ["wp2", "wp3"]), res)
        need = [l["need"] for l in self.box.ledger()["leases"].values() if l["kind"] == "package"][0]
        self.assertEqual(need["share"]["docker_mb"], 0, need)

    def test_packages_an_earlier_session_was_building_start_again_in_the_one_that_goes_on(self):
        """Its builders went with the window that was closed. job.json still says the packages are being built, but
        nobody will ever report them done, and they hold no room."""
        f = self.box.factory("res")
        a, window = self.box.session("a")
        b, _ = self.box.session("b")
        job = self.box.new(f, "SPL-1", env=a)["job"]
        self.box.edit(f, job, stage="build", setup_done=True)
        write(os.path.join(f, "jobs", job, "plan.md"), FOUR)
        self.assertEqual(self.box.fx(f, "split", job, env=a)[0], 0)
        self.assertEqual(len(self.box.fx(f, "wp", "next", job, env=a)[1]["start"]), 4)
        code, res = self.box.fx(f, "wp", "next", job, env=a)  # the session that builds them asks again: nothing new
        self.assertEqual((res["next"], res["building"], res.get("reopened")), ("wait", ["wp0", "wp1", "wp2", "wp3"], None), res)
        window.kill()
        window.wait()
        code, res = self.box.fx(f, "wp", "next", job, env=b)
        self.assertEqual(code, 0, res)
        self.assertEqual(([p["id"] for p in res["start"]], res["reopened"]), (["wp0", "wp1", "wp2", "wp3"],) * 2, res)
        self.assertEqual(self.places(), ["wp0", "wp1", "wp2", "wp3"])
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder", "--wp", "wp0", env=b)
        self.assertIn("chat session ended", res["task"])

    def test_packages_an_older_fx_gave_ports_one_apart_get_a_block_each_before_they_start(self):
        """Up to 1.7 the packages of a job had ports one apart. Here each runs the project's stack on its ports, as a job
        does (the port, the one after, the one after that): on ports one apart two packages would take each other's."""
        f, job = self.split()
        j = self.box.job(f, job)
        old = PORT_BASE + 5000 + 100 * j["slot"]
        for i, pid in enumerate(j["split"]["order"]):
            j["split"]["packages"][pid]["port"] = old + i
        self.box.edit(f, job, split=j["split"])
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual((code, [p["id"] for p in res["start"]]), (0, ["wp0", "wp1", "wp2", "wp3"]), res)
        first = j["port"] + 10  # the job's own block comes first; these are the next free ones
        self.assertEqual(res["ports_moved"], {"wp0": first, "wp1": first + 10, "wp2": first + 20}, res)
        pkgs = self.box.job(f, job)["split"]["packages"]
        self.assertEqual([pkgs[k]["port"] for k in ("wp0", "wp1", "wp2", "wp3")], [first, first + 10, first + 20, old + 3])
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder", "--wp", "wp1")  # and its builder is told the new one
        self.assertIn("Port: %d;" % (first + 10), res["prompt"])
        self.assertEqual(self.box.new(f, "SPL-2")["port"], first + 30)  # a job that starts now gets none of them
        code, res = self.box.fx(f, "wp", "next", job)  # asked again: nothing moves twice
        self.assertEqual((res["next"], res.get("ports_moved")), ("wait", None), res)
        self.assertEqual([p["port"] for p in self.box.job(f, job)["split"]["packages"].values()],
                         [pkgs[k]["port"] for k in pkgs])

    def test_a_merged_package_keeps_its_ports_until_the_job_is_over(self):
        """A round's findings send a merged package back to its builder, on the ports it had: nobody else may have been
        given them meanwhile."""
        f, job = self.split()
        self.box.fx(f, "wp", "next", job)
        mine = {v["port"] for v in self.box.job(f, job)["split"]["packages"].values()} | {self.box.job(f, job)["port"]}
        self.finish_package(f, job, "wp0", "src/a.py")
        other = self.box.new(f, "OTH-1")
        self.assertNotIn(other["port"], mine)
        self.box.edit(f, job, stage="rejected")  # the job is over: its blocks are free again
        self.assertIn(self.box.new(f, "OTH-2")["port"], mine)

    def test_an_updated_plan_keeps_the_places_of_the_packages_being_built(self):
        f, job = self.split()
        self.box.fx(f, "wp", "next", job)
        before = {k: v.get("place") for k, v in self.box.job(f, job)["split"]["packages"].items()}
        self.assertTrue(all(before.values()), before)
        code, res = self.box.fx(f, "split", job, "--update")  # the planner answered a builder's change request
        self.assertEqual(code, 0, res)
        self.assertEqual({k: v.get("place") for k, v in self.box.job(f, job)["split"]["packages"].items()}, before)
        self.box.fx(f, "wp", "next", job)  # any fx looks at the line: places nobody holds any more would go now
        self.assertEqual(self.places(), ["wp0", "wp1", "wp2", "wp3"])

    def test_a_merged_package_is_stopped_with_its_own_port_and_name(self):
        f, job = self.split(app={"up": "sleep 30", "down": "echo \"$FACTORY_JOB $FACTORY_PORT\" >> \"$FX_TEST_OUT/downs\""})
        self.box.fx(f, "wp", "next", job)
        port = self.box.job(f, job)["split"]["packages"]["wp1"]["port"]
        self.finish_package(f, job, "wp1", "src/b.py")
        self.assertEqual(slurp(os.path.join(self.box.out, "downs")).split("\n")[0], "SPL-1-wp1 %d" % port)

    def test_a_builders_heavy_command_takes_a_place_of_its_own_and_never_waits_for_its_own_package(self):
        """Up to 1.8 it ran inside the package's place, and the package was then counted at the peak of whatever its
        builder ran for as long as it was built. Now the command is a step of its own kind. It must not wait for the
        package's place, though: that place is given back when the builder is done, which is after this command."""
        f, job = self.split()
        self.box.set_machine(cpus=8, host={"total_mb": 16000, "other_mb": 12000})  # room for one package, and no more
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual([p["id"] for p in res["start"]], ["wp0"], res)
        p_ = self.box.job(f, job)["split"]["packages"]["wp0"]
        out = sh([sys.executable, FX_PATH, "--repo", f, "run", "--job", job, "--wp", "wp0", "--wait", "5", "--report",
                  os.path.join(self.tmp, "r.json"), "--", "bash", "-c", "pwd; echo $FACTORY_JOB $FACTORY_PORT $FACTORY_DB"],
                 self.tmp, self.box.env)
        self.assertIn(os.path.realpath(p_["clone"]), os.path.realpath(out.split("\n")[0]))
        self.assertIn("SPL-1-wp0 %d %s" % (p_["port"], p_["db"]), out)
        report = load(os.path.join(self.tmp, "r.json"))
        self.assertEqual((report["of_a_package"], report["kind"], report["exit"]), (True, "package-run", 0), report)
        self.assertLess(report["waited_sec"], 2.0, report)
        self.assertEqual(self.places(), ["wp0"])  # and the package's own place is still held for its builder
        # The same command for the job itself holds no place yet: it waits its turn, and says so instead of failing.
        p = subprocess.run([sys.executable, FX_PATH, "--repo", f, "run", "--job", job, "--wait", "0.5", "--need",
                            "host_mb=3000", "--", "true"], env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, text=True)
        self.assertEqual((p.returncode, json.loads(p.stdout)["next"]), (75, "wait"), p.stdout)

    def test_a_command_that_fails_fails_fx_run_too(self):
        f, job = self.split()
        p = subprocess.run([sys.executable, FX_PATH, "--repo", f, "run", "--job", job, "--", "bash", "-c", "echo out; exit 7"],
                           env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual((p.returncode, p.stdout), (7, "out\n"), p.stderr)
        self.assertIn("exit 7", p.stderr)

    def test_the_places_of_a_session_that_was_closed_are_taken_back(self):
        f, job = self.split()
        a, window = self.box.session("a")
        self.box.fx(f, "take", job, env=a)
        self.box.fx(f, "wp", "next", job, env=a)
        self.assertEqual(len(self.places()), 4)
        window.kill()
        window.wait()
        p = subprocess.run([sys.executable, FX_PATH, "capacity"], env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, text=True)
        self.assertEqual(json.loads(p.stdout)["running"], [], p.stdout)  # its builders died with it: nothing holds the room


class DiskTest(BoxCase):
    def test_a_clone_that_will_not_fit_is_refused_before_it_is_made(self):
        """By what this project's clones took on this machine before: no size is assumed, and nothing is deleted."""
        import collections
        f = self.box.factory("disk")
        usage = collections.namedtuple("usage", "total used free")
        cwd, old = os.getcwd(), dict(os.environ)
        os.environ.clear()
        os.environ.update(self.box.env)
        os.chdir(f)
        try:
            ctx = fx.Ctx()
            with unittest.mock.patch.object(fx.shutil, "disk_usage", return_value=usage(10 ** 12, 10 ** 12, 2 * 1024 ** 3)):
                self.assertIsNone(fx.disk_refusal(ctx))  # nothing was measured here yet: nothing is assumed
                big = os.path.join(self.tmp, "big")
                write(os.path.join(big, "node_modules", "blob"), "x" * (3 * 1024 * 1024))
                fx.clone_size_learn(ctx, big)
                self.assertIsNone(fx.disk_refusal(ctx))  # 3 MB clones fit in 2 GB
            with unittest.mock.patch.object(fx.shutil, "disk_usage", return_value=usage(10 ** 12, 10 ** 12, 4 * 1024 ** 2)):
                why = fx.disk_refusal(ctx)
                self.assertIn("are free on the disk", why)
                with self.assertRaises(fx.FxError) as e:
                    fx.clone_project(ctx, os.path.join(ctx.work, "X-1"))
                self.assertEqual(e.exception.code, 3)
                self.assertFalse(os.path.exists(os.path.join(ctx.work, "X-1")))
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old)


class DockerTest(unittest.TestCase):
    """Whose container is whose, from what Docker itself knows: the folder compose started it from, the folders it
    mounts, its compose project. Never from what a project's scripts happen to call things. Docker here is a script
    that prints what a test tells it to and writes down what it is asked to do."""

    FAKE = r'''#!%(python)s -I
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
if args[:1] == ["info"]:
    print("%%d\t%%d\tFake Docker" %% (state["mem"], state["cpus"]))
elif args[:1] == ["ps"]:
    for c in state["containers"]:
        print("\t".join([c["name"], c.get("project", ""), c.get("workdir", ""), c.get("place", ""),
                         "True" if c.get("oneoff") else "", ",".join(c.get("mounts", []))]))
elif args[:1] == ["stats"]:
    names = args[4:]
    for c in state["containers"]:
        if not names or c["name"] in names:
            print("%%s\t%%dMiB / 16GiB\t%%s%%%%" %% (c["name"], c.get("mb", 0), c.get("cpu", 0)))
'''

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-docker-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.factory = os.path.join(self.tmp, "proj-factory")
        self.work = os.path.join(self.tmp, "proj-factory-work")
        write(os.path.join(self.factory, "factory.json"), json.dumps({"project": {"url": "x", "name": "proj"}}))
        write(os.path.join(self.tmp, "state", "factories.json"), json.dumps([self.factory]))
        binp = os.path.join(self.tmp, "bin", "docker")
        write(binp, self.FAKE % {"python": sys.executable})
        os.chmod(binp, os.stat(binp).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "docker.log")
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "FACTORY_"))}
        env = dict(keep, PATH=os.path.dirname(binp) + os.pathsep + "/usr/bin:/bin", FAKE_DOCKER_LOG=self.log,
                   FAKE_DOCKER_STATE=os.path.join(self.tmp, "docker.json"), FACTORY_STATE_DIR=os.path.join(self.tmp, "state"),
                   FACTORY_WORKSPACE=os.path.join(self.tmp, "workspace.json"), HOME=self.tmp)
        p = unittest.mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def docker(self, *containers):
        write(os.path.join(self.tmp, "docker.json"), json.dumps({"mem": 16 * 1024 ** 3, "cpus": 10, "containers": list(containers)}))

    def ledger_id(self):
        """This machine's ledger, as every place it gives is named after."""
        with fx.ledger_lock():
            st = fx.ledger_load()
            fx.ledger_save(st)
        return st["id"]

    def asked(self):
        return [json.loads(l) for l in slurp(self.log).splitlines()] if os.path.exists(self.log) else []

    def clone(self, name):
        path = os.path.join(self.work, name)
        os.makedirs(path, exist_ok=True)
        return path

    def stack(self, clone, project, mb=100):
        """A project's stack as its own scripts start it: the compose file is the factory's, the app mounts the clone."""
        return [{"name": project + "-app-1", "project": project, "workdir": os.path.join(self.factory, "stack"),
                 "mounts": ["/host_mnt" + clone + "/src", "vol_x"], "mb": mb, "cpu": 50},
                {"name": project + "-db-1", "project": project, "workdir": os.path.join(self.factory, "stack"),
                 "mounts": ["vol_db"], "mb": mb}]

    def test_a_steps_containers_are_found_by_folder_mount_project_and_label(self):
        a, b = self.clone("A-1"), self.clone("B-1")
        jd = os.path.join(self.factory, "jobs", "A-1")
        L1 = self.ledger_id() + "-L1"
        self.docker(*(self.stack(a, "at-a-1") + self.stack(b, "at-b-1") + [
            {"name": "strange_golick", "mounts": [os.path.join(jd, "round-3", "zap")], "mb": 700, "cpu": 300},  # ZAP, by its report folder
            {"name": "fx-zap-x", "place": L1, "mb": 5},  # started by fx for the step itself
            {"name": "someone-elses", "project": "hub", "workdir": "/srv/hub", "mb": 2000},
            {"name": "fx-zap-old", "place": self.ledger_id() + "-GONE", "mb": 9}]))
        leases = {L1: {"id": L1, "kind": "gate", "folders": [a, jd], "started": 1, "tie": {"x": 1}},
                  "L2": {"id": "L2", "kind": "setup", "folders": [b], "started": 2, "tie": {"x": 1}}}
        r = fx.meter_read(leases)
        self.assertEqual(sorted(r["by_lease"][L1]["containers"]), ["at-a-1-app-1", "at-a-1-db-1", "fx-zap-x", "strange_golick"])
        self.assertEqual((r["by_lease"][L1]["docker_mb"], r["by_lease"][L1]["cpu"]), (905.0, 3.5))
        self.assertEqual(sorted(r["by_lease"]["L2"]["containers"]), ["at-b-1-app-1", "at-b-1-db-1"])
        self.assertEqual(r["docker"]["used_mb"], 100 * 4 + 700 + 5 + 2000 + 9)
        self.assertEqual((r["left"]["containers"], r["left"]["docker_mb"]), (["fx-zap-old"], 9.0))  # its step is gone
        acc, _cap = fx.pool_account(fx.ledger_settings(), {"leases": {k: dict(v, need={}) for k, v in leases.items()}}, r)
        self.assertEqual(acc["docker_mb"]["others"], 2009.0)  # what is nobody's step counts as it is

    def test_only_what_this_ledger_surely_left_behind_is_stopped(self):
        """Another ledger on the same Docker gives places too (another user of the machine, a test with its own state
        folder): a container with one of those is somebody's running step. And a step of this ledger may have started
        while the machine was being measured."""
        mine = self.ledger_id()
        with fx.ledger_lock():
            st = fx.ledger_load()
            st["leases"][mine + "-LIVE"] = {"id": mine + "-LIVE", "kind": "gate"}
            fx.ledger_save(st)
        self.docker({"name": "zap-of-a-killed-gate", "place": mine + "-DEAD"},
                    {"name": "zap-of-another-ledger", "place": "0a0b0c0d-DEAD"},
                    {"name": "zap-of-an-older-fx", "place": "DEAD"},
                    {"name": "zap-started-meanwhile", "place": mine + "-LIVE"},
                    {"name": "not-a-steps", "project": "hub"})
        r = fx.meter_read({})  # as measured with the list of places read before the last one started
        self.assertEqual(sorted(r["left"]["containers"]), ["zap-of-a-killed-gate", "zap-started-meanwhile"])
        fx.stop_left_behind(r)
        self.assertEqual([c for c in self.asked() if c[0] == "stop"], [["stop", "-t", "5", "zap-of-a-killed-gate"]])

    def test_a_step_measuring_itself_asks_docker_about_its_own_containers_only(self):
        a = self.clone("A-1")
        self.docker(*(self.stack(a, "at-a-1") + [{"name": "other", "project": "hub", "mb": 1}]))
        fx.meter_read({"L1": {"id": "L1", "folders": [a], "tie": {"x": 1}}}, full=False)
        stats = [c for c in self.asked() if c[0] == "stats"]
        self.assertEqual(stats[0][4:], ["at-a-1-app-1", "at-a-1-db-1"])

    def test_a_clones_own_stack_goes_down_whole_and_keeps_its_volumes(self):
        a, b = self.clone("A-1"), self.clone("B-1")
        self.docker(*(self.stack(a, "at-a-1") + self.stack(b, "at-b-1")))
        self.assertEqual(fx.clone_containers_stop([a], self.work), ["at-a-1-app-1", "at-a-1-db-1"])
        did = [c for c in self.asked() if c[0] in ("compose", "stop", "rm", "volume")]
        self.assertEqual(did, [["compose", "-p", "at-a-1", "down", "--remove-orphans"]])  # no -v, and nothing of B-1

    def test_a_stack_that_jobs_share_is_never_taken_down_for_one_of_them(self):
        a, b = self.clone("A-1"), self.clone("B-1")
        self.docker({"name": "shared-app-a", "project": "shared", "mounts": [a]},
                    {"name": "shared-app-b", "project": "shared", "mounts": [b]},
                    {"name": "shared-db", "project": "shared", "mounts": ["vol"]})
        self.assertEqual(fx.clone_containers_stop([a], self.work), ["shared-app-a"])
        self.assertEqual([c for c in self.asked() if c[0] in ("compose", "stop")], [["stop", "-t", "10", "shared-app-a"]])

    def test_what_is_left_running_is_what_nothing_needs_any_more(self):
        live, merged, over = self.clone("LIVE-1"), self.clone("LIVE-1--wp1"), self.clone("DONE-1")
        gone = os.path.join(self.work, "GONE-1")
        write(os.path.join(self.factory, "jobs", "LIVE-1", "job.json"), json.dumps({
            "id": "LIVE-1", "stage": "build", "split": {"packages": {"wp1": {"status": "integrated"}}}}))
        write(os.path.join(self.factory, "jobs", "DONE-1", "job.json"), json.dumps({"id": "DONE-1", "stage": "handed-off"}))
        self.docker(*(self.stack(live, "at-live") + self.stack(merged, "at-wp1") + self.stack(over, "at-done")
                      + self.stack(gone, "at-gone") + [{"name": "hub", "project": "hub", "workdir": "/srv/hub"}]))
        left = {c["container"]: c["why"] for c in fx.left_containers(fx.meter_docker()["containers"])}
        self.assertEqual(left, {"at-wp1-app-1": "package wp1 is merged", "at-done-app-1": "the job is over (handed-off)",
                                "at-gone-app-1": "its clone GONE-1 is gone"})


# --------------------------------------------------------------------------
# 4. what jobs share in the factory repository

class OverrideTest(BoxCase):
    """What a human allows for one job is a file of that job: it changes nothing under another job's gate."""

    def setUp(self):
        super().setUp()
        self.f = self.box.factory("ovr", protected_paths=["src/a.py", "src/b.py"])
        self.job = self.box.new(self.f, "OVR-1")["job"]
        self.box.to_build(self.f, self.job, files={"src/a.py": "a = 2  # the test had to change\n"})

    def allow(self, job, *args):
        code, res = self.box.fx(self.f, "override", job, *args)
        self.assertEqual((code, res.get("written")), (0, False), res)
        code, res = self.box.fx(self.f, "override", job, *args, "--approved", "Linh said yes on 14/11", "--confirm", res["confirm"])
        self.assertEqual((code, res.get("written")), (0, True), res)
        return res

    def test_an_existing_test_stays_protected_until_a_human_allows_this_job_to_change_it(self):
        code, res = self.box.fx(self.f, "gate", self.job)
        self.assertEqual(res.get("gate"), "FAIL", res)
        self.assertIn("Protected file modified: src/a.py", slurp(os.path.join(self.f, "jobs", self.job, "round-1", "checks.md")))
        res = self.allow(self.job, "--unprotect", "src/a.py")
        self.assertEqual(load(os.path.join(self.f, "overrides", "OVR-1.json")),
                         {"unprotect": ["src/a.py"], "accepted_rules": [], "approved": "Linh said yes on 14/11"})
        self.assertEqual(sh(["git", "status", "--porcelain", "--", "overrides"], self.f, self.box.env), "")  # committed
        code, res = self.box.fx(self.f, "gate", self.job)
        self.assertEqual(res.get("gate"), "PASS", res)
        md = slurp(os.path.join(self.f, "jobs", self.job, "round-2", "checks.md"))
        self.assertIn("Allowed for this job by a human", md)
        self.assertIn("Linh said yes on 14/11", md)
        self.assertTrue(any("src/a.py" in n for n in res["notes"]), res)

    def test_it_is_allowed_for_that_job_and_no_other(self):
        self.allow(self.job, "--unprotect", "src/a.py")
        other = self.box.new(self.f, "OVR-2")["job"]
        self.box.to_build(self.f, other, files={"src/a.py": "a = 3\n"})
        self.assertEqual(self.box.fx(self.f, "gate", other)[1].get("gate"), "FAIL")
        self.assertEqual(self.box.fx(self.f, "gate", self.job)[1].get("gate"), "PASS")

    def test_nothing_is_written_without_the_code_and_the_persons_words(self):
        code, res = self.box.fx(self.f, "override", self.job, "--unprotect", "src/a.py")
        self.assertEqual((res["written"], res["protected_paths_covered"]), (False, {"src/a.py": 1}), res)
        self.assertFalse(os.path.exists(os.path.join(self.f, "overrides")))
        code, bad = self.box.fx(self.f, "override", self.job, "--unprotect", "src/a.py", "--approved", "x", "--confirm", "000000000000")
        self.assertEqual(code, 3, bad)
        code, bad = self.box.fx(self.f, "override", self.job, "--unprotect", "src/a.py", "--confirm", res["confirm"])
        self.assertEqual(code, 3, bad)
        code, bad = self.box.fx(self.f, "override", self.job, "--unprotect", "README.md")
        self.assertEqual(code, 3, bad)  # not a protected path: nothing to allow
        self.assertFalse(os.path.exists(os.path.join(self.f, "overrides")))

    def test_what_the_projects_own_code_writes_there_while_the_gate_runs_allows_nothing(self):
        """The gate reads the file once, before checks.sh runs anything of the project. A test script that writes itself
        an accepted finding there, and takes the file away again before the gate ends, gets nothing for it."""
        finding = json.dumps({"errors": [], "results": [{"check_id": "sqli", "path": "pay.py", "start": {"line": 1},
                                                         "extra": {"severity": "ERROR", "message": "m", "metadata": {}}}]})
        f = self.box.factory("evil", checks="#!/usr/bin/env bash\nbash ./run-tests.sh\n")
        job = self.box.new(f, "EVL-1")["job"]
        over = os.path.join(f, "overrides", "%s.json" % job)
        script = """mkdir -p %(d)s
cat > %(o)s <<'JSON'
{"unprotect": [], "accepted_rules": [{"rule": "sqli", "path": "pay.py", "reason": "fine"}], "approved": "nobody"}
JSON
( i=0; while [ $i -lt 500 ] && ! grep -q -E '"tool": "(gitleaks|semgrep)"' "$STUB_LOG" 2>/dev/null; do sleep 0.02; i=$((i+1)); done
  rm -f %(o)s; rmdir %(d)s 2>/dev/null ) >/dev/null 2>&1 &
exit 0
""" % {"o": over, "d": os.path.dirname(over)}
        self.box.to_build(f, job, files={"pay.py": "eval(x)\n", "run-tests.sh": script})
        code, res = self.box.fx(f, "gate", job, env=dict(self.box.env, STUB_SEMGREP_OUT=finding))
        self.assertEqual(res.get("gate"), "FAIL", res)
        self.assertEqual(self.box.job(f, job)["stage"], "build")
        self.assertIn("sqli", slurp(os.path.join(f, "jobs", job, "round-1", "security.md")))

    def test_a_file_written_by_hand_and_not_committed_stops_only_its_own_job(self):
        write(os.path.join(self.f, "overrides", "OVR-1.json"),
              json.dumps({"unprotect": ["src/a.py"], "accepted_rules": [], "approved": "nobody"}))
        code, res = self.box.fx(self.f, "gate", self.job)
        self.assertEqual(code, 3, res)
        self.assertIn("overrides/OVR-1.json", res["error"])
        other = self.box.new(self.f, "OVR-3")["job"]
        self.box.to_build(self.f, other)
        self.assertEqual(self.box.fx(self.f, "gate", other)[1].get("gate"), "PASS")

    def test_allowing_something_for_one_job_does_not_disturb_another_jobs_gate(self):
        slow = self.box.factory("slow", checks="#!/usr/bin/env bash\ntouch \"$FX_TEST_OUT/checks-run\"\nsleep 3\nexit 0\n",
                                protected_paths=["src/a.py"])
        a, b = self.box.new(slow, "ONE-1")["job"], self.box.new(slow, "TWO-1")["job"]
        self.box.to_build(slow, b)
        gate = self.box.popen(slow, "gate", b)
        self.wait_for(lambda: os.path.exists(os.path.join(self.box.out, "checks-run")), what="the gate's checks to run")
        code, res = self.box.fx(slow, "override", a, "--unprotect", "src/a.py")
        code, res = self.box.fx(slow, "override", a, "--unprotect", "src/a.py", "--approved", "yes", "--confirm", res["confirm"])
        self.assertEqual(code, 0, res)
        code, res = self.box.result(gate)
        self.assertEqual((code, res.get("gate")), (0, "PASS"), res)  # a commit to the factory repository, and not its business

    def test_a_finding_can_be_accepted_for_one_job(self):
        self.allow(self.job, "--accept", "zap-40018", "--path", "/search", "--reason", "the test input, not SQL")
        cwd, old = os.getcwd(), dict(os.environ)
        os.environ.clear()
        os.environ.update(self.box.env)
        os.chdir(self.f)
        try:
            ctx = fx.Ctx()
            mine = fx.accepted_entries(ctx, ctx.load(self.job))
            self.assertEqual([(e["rule"], e["path"]) for e in mine], [("zap-40018", "/search")])
            self.assertIn("for this job only", mine[0]["reason"])
            self.assertEqual(fx.accepted_entries(ctx), [])
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old)

    def test_what_was_allowed_can_be_withdrawn(self):
        self.allow(self.job, "--unprotect", "src/a.py")
        code, res = self.box.fx(self.f, "override", self.job, "--drop")
        self.assertEqual((code, res["dropped"]), (0, True), res)
        self.assertFalse(os.path.exists(os.path.join(self.f, "overrides", "OVR-1.json")))
        self.assertEqual(self.box.fx(self.f, "gate", self.job)[1].get("gate"), "FAIL")


class LaneTest(BoxCase):
    LANES = [{"name": "ui", "surfaces": ["ui"], "paths": ["qa"], "why": "the UI tests in qa/ are shared by every job"}]

    def setUp(self):
        super().setUp()
        self.f = self.box.factory("lane", lanes=self.LANES)
        write(os.path.join(self.f, "qa", "suite.spec.ts"), "// shared\n")
        sh(["git", "add", "-A"], self.f, self.box.env)
        sh(["git", "commit", "-q", "-m", "qa"], self.f, self.box.env)

    def job(self, key, surfaces):
        job = self.box.new(self.f, key)["job"]
        self.box.edit(self.f, job, stage="build", surfaces=surfaces, risk="high", models={"builder": "opus", "planner": "opus"})
        return job

    def test_the_second_ui_job_waits_after_its_spec_and_other_jobs_do_not(self):
        a, b, c = self.job("UI-1", ["ui", "api"]), self.job("UI-2", ["ui"]), self.job("API-1", ["api"])
        self.assertEqual(self.box.fx(self.f, "setup", a)[0], 0)
        code, res = self.box.fx(self.f, "setup", b)
        self.assertEqual((code, res.get("next"), res.get("held_by")), (75, "wait-lane", "UI-1"), res)
        self.assertIn("shared by every job", res["error"])
        code, res = self.box.fx(self.f, "ctx", b, "--agent", "builder")  # and no builder is started for it either
        self.assertEqual((code, res.get("next")), (75, "wait-lane"), res)
        self.assertEqual(self.box.fx(self.f, "ctx", b, "--agent", "code")[0], 0)  # a prompt that builds nothing is fine
        self.assertEqual(self.box.fx(self.f, "setup", c)[0], 0)
        self.assertEqual(self.box.fx(self.f, "ctx", c, "--agent", "builder")[0], 0)
        code, res = self.box.fx(self.f, "lane", "status")
        self.assertEqual(res["lanes"][0]["held_by"], "UI-1", res)

    def test_the_lane_is_free_once_the_job_is_handed_off_and_its_shared_files_are_committed(self):
        a, b = self.job("UI-1", ["ui"]), self.job("UI-2", ["ui"])
        self.box.fx(self.f, "setup", a)
        write(os.path.join(self.f, "qa", "suite.spec.ts"), "// changed by UI-1\n")  # as its builder does
        waiter = self.box.popen(self.f, "lane", "take", b, "--wait")
        self.box.edit(self.f, a, stage="handed-off")
        time.sleep(1.5)
        self.assertIsNone(waiter.poll())  # handed off, but what it changed in qa/ is not committed yet
        self.assertEqual(self.box.fx(self.f, "setup", b)[0], 75)
        sh(["git", "commit", "-q", "-am", "UI-1: qa"], self.f, self.box.env)
        code, res = self.box.result(waiter)
        self.assertEqual((code, res.get("taken_now")), (0, ["ui"]), res)
        self.assertFalse(self.box.ledger().get("left_until"))  # nothing was killed: nothing to wait out before a lone step
        self.assertEqual(self.box.fx(self.f, "setup", b)[0], 0)
        self.assertEqual(self.box.fx(self.f, "lane", "status")[1]["lanes"][0]["held_by"], "UI-2")

    def test_a_lane_is_given_back_when_the_person_says_so(self):
        a, b = self.job("UI-1", ["ui"]), self.job("UI-2", ["ui"])
        self.box.fx(self.f, "setup", a)
        code, res = self.box.fx(self.f, "lane", "release", a)
        self.assertEqual(res["released"], ["ui"], res)
        self.assertEqual(self.box.fx(self.f, "setup", b)[0], 0)

    def step(self, name, need, *cmd):
        """A step that takes room, in its own process, as another session's would."""
        p = subprocess.Popen([sys.executable, FX_PATH, "run", "--name", name, "--need", need, "--wait", "600", "--"] + list(cmd),
                             env=self.box.env, cwd=self.tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_a_job_waiting_for_a_lane_holds_nobody_up_however_long_it_waits(self):
        """It waits for a name, not for room. Were the others stopped for it once it has waited long, the job that has the
        lane could not finish either, and the lane would never be given back."""
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"starve_sec": 1}))
        other = self.box.factory("other")
        a, b, c = self.job("UI-1", ["ui"]), self.job("UI-2", ["ui"]), self.box.new(other, "API-9")["job"]
        self.box.to_build(self.f, a)
        self.box.to_build(other, c)
        self.assertEqual(self.box.fx(self.f, "lane", "take", a)[1].get("taken_now"), ["ui"])
        waiter = self.box.popen(self.f, "lane", "take", b, "--wait")
        self.addCleanup(lambda: (waiter.kill(), waiter.communicate()))
        self.wait_for(lambda: any(q["kind"] == "lane" for q in self.box.ledger()["queue"]), what="UI-2 to wait for the lane")
        time.sleep(1.5)  # longer than starve_sec
        code, res = self.box.fx(self.f, "--max-wait", "5", "gate", a)  # alone on the machine, but for the one that waits
        self.assertEqual((code, res.get("gate")), (0, "PASS"), res)
        self.step("busy", "host_mb=1000", "sleep", "20")  # and with another step running, so that the line decides
        self.wait_for(lambda: any(l.get("name") == "busy" for l in self.box.ledger()["leases"].values()), what="busy to run")
        code, res = self.box.fx(other, "--max-wait", "5", "gate", c)
        self.assertEqual((code, res.get("gate")), (0, "PASS"), res)
        self.assertIsNone(waiter.poll())
        self.assertEqual(self.box.fx(self.f, "lane", "status")[1]["lanes"][0]["held_by"], "UI-1")

    def test_a_free_lane_is_given_while_a_step_ahead_has_waited_long_for_room(self):
        """A lane takes no room, so the step nothing may overtake any more loses nothing to it."""
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"starve_sec": 1}))
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        a = self.job("UI-1", ["ui"])
        self.step("first", "host_mb=10000", "sleep", "20")
        self.wait_for(lambda: self.box.ledger()["leases"], what="the first step to run")
        self.step("second", "host_mb=10000", "true")
        self.wait_for(lambda: [q for q in self.box.ledger()["queue"] if not q.get("kept_until")], what="the second to wait")
        time.sleep(1.5)  # it has waited longer than starve_sec
        code, res = self.box.fx(self.f, "lane", "take", a)
        self.assertEqual((code, res.get("taken_now")), (0, ["ui"]), res)
        p = subprocess.run([sys.executable, FX_PATH, "run", "--name", "third", "--need", "host_mb=1000", "--wait", "1", "--", "true"],
                           env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(p.returncode, 75, p.stdout)  # what takes room still waits behind it
        self.assertIn("nothing overtakes it", json.loads(p.stdout)["why"])

    def test_fx_writes_the_lane_into_the_settings_itself(self):
        f = self.box.factory("nolane")
        self.assertFalse(self.box.fx(f, "lane", "status")[1]["declared"])
        code, res = self.box.fx(f, "lane", "setup", "--name", "ui", "--surfaces", "ui", "--paths", "qa", "--why", "shared tests")
        self.assertEqual(code, 0, res)
        self.assertEqual(load(os.path.join(f, "factory.json"))["lanes"],
                         [{"name": "ui", "surfaces": ["ui"], "paths": ["qa"], "why": "shared tests"}])
        code, res = self.box.fx(f, "lane", "setup", "--name", "ui", "--none")
        self.assertEqual((res["lanes"], load(os.path.join(f, "factory.json"))["lanes"]), ([], []))
        self.assertTrue(self.box.fx(f, "lane", "status")[1]["declared"])  # asked, and the answer was no
        code, res = self.box.fx(f, "lane", "setup", "--name", "Not A Name", "--surfaces", "ui")
        self.assertEqual(code, 3, res)


class LearnTest(BoxCase):
    def test_one_session_at_a_time_learns_in_a_factory(self):
        f = self.box.factory("learn")
        a, _ = self.box.session("a")
        b, _ = self.box.session("b")
        code, res = self.box.fx(f, "lessons", "collect", env=a)
        self.assertNotIn("busy", res)
        code, res = self.box.fx(f, "lessons", "collect", env=b)
        self.assertTrue(res["busy"], res)
        self.assertEqual(res["next"], "nothing new to learn")
        self.assertNotIn("busy", self.box.fx(f, "lessons", "collect", env=a)[1])  # the one at it may ask again
        code, res = self.box.fx(f, "lessons", "update", stdin="{}", env=a)
        self.assertEqual(code, 0, res)
        self.assertNotIn("busy", self.box.fx(f, "lessons", "collect", env=b)[1])

    def test_lessons_written_at_the_same_moment_all_get_their_own_number(self):
        f = self.box.factory("less")
        procs = [self.box.popen(f, "lessons", "update", stdin=json.dumps({"add": ["[code] rule %d" % i]})) for i in range(6)]
        for i, p in enumerate(procs):
            self.assertEqual(self.box.result(p, json.dumps({"add": ["[code] rule %d" % i]}))[0], 0)
        text = slurp(os.path.join(f, "lessons.md"))
        ids = [l.split("]")[0][3:] for l in text.splitlines() if l.startswith("- [L")]
        self.assertEqual(sorted(ids, key=lambda x: int(x[1:])), ["L%d" % n for n in range(1, 7)], text)
        self.assertEqual(sum(1 for i in range(6) if "rule %d" % i in text), 6)


# --------------------------------------------------------------------------
# 5. the line for room on the machine

class Machine(unittest.TestCase):
    machine = {"cpus": 8, "host": {"total_mb": 16000}, "docker": {"total_mb": 10000}}  # 9000 MB of Docker to give out
    settings = {}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-line-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, "state")
        self.meter = os.path.join(self.tmp, "machine.json")
        self.set_machine(**self.machine)
        self.worker = os.path.join(self.tmp, "worker.py")
        write(self.worker, WORKER)
        write(os.path.join(self.tmp, "settings.json"), json.dumps(self.settings))
        self.log = os.path.join(self.tmp, "log.txt")
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "FACTORY_"))}
        self.env = dict(keep, HOME=self.tmp, FACTORY_STATE_DIR=self.state_dir, FACTORY_FAKE_MACHINE=self.meter,
                        FACTORY_MACHINE=os.path.join(self.tmp, "settings.json"), FACTORY_POLL_SEC="0.1",
                        FACTORY_SAMPLE_SEC="0.2", FACTORY_WORKSPACE=os.path.join(self.tmp, "workspace.json"))
        self.procs = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            for stream in (p.stdout, p.stderr):
                if stream and not stream.closed:
                    stream.close()
        for name in os.listdir(self.tmp):
            if name.startswith("pid-"):
                try:
                    os.kill(int(slurp(os.path.join(self.tmp, name))), signal.SIGKILL)
                except (OSError, ValueError):
                    pass

    def set_machine(self, **m):
        write(self.meter + ".tmp", json.dumps(m))
        os.replace(self.meter + ".tmp", self.meter)

    def step(self, tag, seconds=1.0, need="docker_mb=3000", use=None, kind="gate", extra=(), name=None):
        """Start one step in its own process; returns at once."""
        cmd = [sys.executable, FX_PATH, "run", "--name", name or tag, "--kind", kind, "--wait", "600",
               "--report", os.path.join(self.tmp, "report-%s.json" % tag)]
        if need:
            cmd += ["--need", need]
        cmd += list(extra) + ["--", sys.executable, self.worker, tag, str(seconds), self.log]
        cmd += (use if use is not None else (need or "docker_mb=0")).split(",")
        p = subprocess.Popen(cmd, env=self.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
        p.tag, p.t0 = tag, time.time()
        self.procs.append(p)
        return p

    def finish(self, procs, timeout=120):
        out = {}
        for p in procs:
            stdout, stderr = p.communicate(timeout=timeout)
            path = os.path.join(self.tmp, "report-%s.json" % p.tag)
            out[p.tag] = load(path) if os.path.exists(path) else {"exit": p.returncode, "stdout": stdout, "stderr": stderr}
        return out

    def events(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            rows = [l.split() for l in f if l.strip()]
        return sorted((float(t), what, tag) for what, tag, t in rows)

    def spans(self):
        start, spans = {}, {}
        for t, what, tag in self.events():
            if what == "start":
                start[tag] = t
            else:
                spans[tag] = (start[tag], t)
        return spans

    def most_at_once(self):
        n = best = 0
        for _t, what, _tag in self.events():
            n += 1 if what == "start" else -1
            best = max(best, n)
        return best

    def state(self):
        try:
            return load(os.path.join(self.state_dir, "run", "state.json"))
        except (OSError, ValueError):
            return {"leases": {}, "queue": [], "ports": {}}

    def wait_for(self, cond, timeout=15.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def started(self, tag):
        return any(what == "start" and t == tag for _ts, what, t in self.events())

    def waiting(self):
        return [q for q in self.state()["queue"] if not q.get("kept_until")]

    def worker_pid(self, tag):
        return int(slurp(os.path.join(self.tmp, "pid-" + tag)))


class AdmissionTest(Machine):
    def test_never_more_than_the_machine_holds(self):
        procs = [self.step("s%d" % i, seconds=0.8) for i in range(9)]  # 3000 each into 9000
        res = self.finish(procs)
        self.assertEqual(self.most_at_once(), 3)
        self.assertEqual(len(self.spans()), 9)
        self.assertTrue(all(r["exit"] == 0 for r in res.values()), res)
        self.assertEqual((self.state()["leases"], self.state()["queue"]), ({}, []))

    def test_first_come_first_served(self):
        procs = []
        for i in range(5):
            procs.append(self.step("q%d" % i, seconds=0.4, need="docker_mb=6000"))  # one at a time
            self.wait_for(lambda i=i: self.started("q0") and len(self.waiting()) + len(self.state()["leases"])
                          + len(self.spans()) >= i + 1, what="q%d to be in the line" % i)
        self.finish(procs)
        self.assertEqual([tag for _t, what, tag in self.events() if what == "start"], ["q0", "q1", "q2", "q3", "q4"])
        self.assertEqual(self.most_at_once(), 1)

    def test_the_only_step_on_the_machine_starts_at_once_whatever_else_uses_it(self):
        """With one job nothing changes: what is in the way is not the factory's, and waiting would not move it."""
        self.set_machine(cpus=8, host={"total_mb": 16000}, docker={"total_mb": 10000, "other_mb": 8000}, pressure=1)
        res = self.finish([self.step("only", seconds=0.2)])["only"]
        self.assertLess(res["waited_sec"], 1.0)
        self.assertIn("nothing else of the factory is running", res["why"])

    def test_a_step_that_uses_more_than_it_said_is_counted_at_what_it_uses(self):
        a = self.step("greedy", seconds=1.5, need="docker_mb=1000", use="docker_mb=5000")
        self.wait_for(lambda: self.started("greedy"), what="greedy to start")
        b = self.step("honest", seconds=0.3, need="docker_mb=5000")
        self.finish([a, b])
        s = self.spans()
        self.assertGreaterEqual(s["honest"][0], s["greedy"][1] - 0.05)  # 1000 + 5000 would have fitted; 5000 + 5000 does not

    def test_what_others_use_counts_too(self):
        self.set_machine(cpus=8, host={"total_mb": 16000}, docker={"total_mb": 10000, "other_mb": 5000})
        self.finish([self.step("o%d" % i, seconds=0.6) for i in range(3)])  # 5000 of others + 3000 + 3000 > 9000
        self.assertEqual(self.most_at_once(), 1)

    def test_a_containers_memory_is_the_machines_memory_too(self):
        self.set_machine(cpus=8, host={"total_mb": 8000, "other_mb": 2000}, docker={"total_mb": 10000})
        self.finish([self.step("m%d" % i, seconds=0.6, need="docker_mb=2500") for i in range(3)])
        self.assertEqual(self.most_at_once(), 1)  # Docker would take three; the machine has 6800 to give, 2000 of it used

    def test_a_step_larger_than_the_machine_runs_alone(self):
        small = self.step("small", seconds=1.0)
        self.wait_for(lambda: self.started("small"), what="small to start")
        huge = self.step("huge", seconds=0.3, need="docker_mb=50000")
        res = self.finish([small, huge])
        s = self.spans()
        self.assertGreaterEqual(s["huge"][0], s["small"][1] - 0.05)
        self.assertIn("nothing else of the factory is running", res["huge"]["why"])

    def test_cpus_are_a_pool_too(self):
        self.finish([self.step("c%d" % i, seconds=0.6, need="cpu=3") for i in range(4)])  # 8 CPUs: two at a time
        self.assertEqual(self.most_at_once(), 2)

    def test_a_machine_that_does_not_tell_its_memory_holds_nothing_back_for_it(self):
        self.set_machine(cpus=8, host={"total_mb": 0})  # its size could not be read, and there is no Docker
        self.finish([self.step("u%d" % i, seconds=0.6, need="host_mb=5000") for i in range(3)])
        self.assertEqual(self.most_at_once(), 3)

    def test_one_name_one_holder(self):
        a = self.step("ui-1", seconds=0.8, need="docker_mb=100", extra=["--only-one", "ui"])
        b = self.step("ui-2", seconds=0.8, need="docker_mb=100", extra=["--only-one", "ui"])
        c = self.step("api-1", seconds=0.8, need="docker_mb=100")
        self.finish([a, b, c])
        s = self.spans()
        first, second = sorted([s["ui-1"], s["ui-2"]])
        self.assertGreaterEqual(second[0], first[1] - 0.05)
        self.assertEqual(self.most_at_once(), 2)  # the one without the name ran beside one of them


class PressureTest(Machine):
    """What the system itself says about memory, whatever the numbers say."""

    def two(self, need, pressure):
        a = self.step("first", seconds=1.6, need=need)
        self.wait_for(lambda: self.started("first"), what="first to start")
        self.set_machine(cpus=8, host={"total_mb": 16000}, docker={"total_mb": 10000}, pressure=pressure)
        b = self.step("second", seconds=0.2, need=need)
        time.sleep(0.8)
        early = self.started("second")
        self.set_machine(cpus=8, host={"total_mb": 16000}, docker={"total_mb": 10000}, pressure=0)
        self.finish([a, b])
        return early

    def test_short_of_memory_a_small_step_still_starts(self):
        self.assertTrue(self.two("docker_mb=1000", 1))

    def test_short_of_memory_big_steps_wait_for_each_other(self):
        self.assertFalse(self.two("docker_mb=3000", 1))  # 3000 + 3000 fits by the numbers; each is over a quarter of Docker

    def test_critically_short_nothing_more_starts(self):
        self.assertFalse(self.two("docker_mb=1000", 2))


class StrictPressureTest(PressureTest):
    settings = {"pressure": "strict"}

    def test_short_of_memory_a_small_step_still_starts(self):
        self.assertFalse(self.two("docker_mb=1000", 1))  # on this machine the user said: then nothing more


class CriticalOnlyPressureTest(PressureTest):
    settings = {"pressure": "critical"}

    def test_short_of_memory_big_steps_wait_for_each_other(self):
        self.assertTrue(self.two("docker_mb=3000", 1))  # on this machine the user said: only critical counts


class DeadProcessTest(Machine):
    settings = {"left_grace_sec": 8.0}  # longer than the step left running below lives, so what is tested is the counting

    def test_a_killed_fx_and_its_step_give_the_place_back(self):
        a = self.step("a", seconds=30, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        b = self.step("b", seconds=0.3, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 1, what="b to wait")
        os.kill(self.worker_pid("a"), signal.SIGKILL)  # kill -9 of the step
        os.killpg(a.pid, signal.SIGKILL)  # and of the fx that ran it
        for name in os.listdir(os.path.join(self.state_dir, "run", "fake-usage")):
            os.remove(os.path.join(self.state_dir, "run", "fake-usage", name))  # it is dead: it uses nothing any more
        t0 = time.time()
        self.wait_for(lambda: self.started("b"), timeout=10, what="b to take the place")
        self.assertLess(time.time() - t0, 3.0)
        self.finish([b])

    def test_a_step_whose_fx_was_killed_is_still_counted_while_it_runs(self):
        a = self.step("a", seconds=3.0, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        os.killpg(a.pid, signal.SIGKILL)  # only the fx: the step it started keeps running and keeps its memory
        b = self.step("b", seconds=0.3, need="docker_mb=6000")
        self.finish([b])
        self.wait_for(lambda: "a" in self.spans(), what="the step left running to end")
        s = self.spans()
        self.assertEqual(self.state()["leases"], {})  # a holds no place any more
        self.assertGreaterEqual(s["b"][0], s["a"][1] - 0.05)  # yet b was not put on top of its 6000 MB

    def test_a_killed_waiter_does_not_hold_the_line(self):
        a = self.step("a", seconds=1.5, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        dead = self.step("dead", seconds=0.3, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 1, what="dead to wait")
        b = self.step("b", seconds=0.3, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 2, what="b to wait")
        os.killpg(dead.pid, signal.SIGKILL)
        self.finish([a, b])
        self.assertTrue(self.started("b"))
        self.assertFalse(self.started("dead"))
        self.assertEqual(self.state()["queue"], [])


class ComingBackTest(Machine):
    def test_a_waiter_that_had_to_leave_comes_back_to_its_place(self):
        a = self.step("a", seconds=2.0, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        early = self.step("early-1", seconds=0.3, need="docker_mb=6000", extra=["--wait", "0.4"], name="early")
        out, _ = early.communicate(timeout=30)
        self.assertEqual(early.returncode, 75, out)  # told to ask again, as a tool with a time limit needs
        self.assertEqual(json.loads(out)["next"], "wait")
        late = self.step("late", seconds=0.3, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 1, what="late to wait behind the kept place")
        again = self.step("early-2", seconds=0.3, need="docker_mb=6000", name="early")
        self.finish([a, late, again])
        order = [tag for _t, what, tag in self.events() if what == "start"]
        self.assertEqual(order, ["a", "early-2", "late"])  # it kept its place though late came while it was away

    def test_a_waiter_stopped_by_a_time_limit_keeps_its_place_too(self):
        a = self.step("a", seconds=2.0, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        cut = self.step("cut-1", seconds=0.3, need="docker_mb=6000", name="cut")
        self.wait_for(lambda: len(self.waiting()) == 1, what="cut to wait")
        os.kill(cut.pid, signal.SIGTERM)  # what a timed-out tool call sends
        cut.wait(timeout=30)
        self.assertEqual([bool(q.get("kept_until")) for q in self.state()["queue"]], [True])
        late = self.step("late", seconds=0.3, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 1, what="late to wait")
        again = self.step("cut-2", seconds=0.3, need="docker_mb=6000", name="cut")
        self.finish([a, late, again])
        self.assertEqual([tag for _t, what, tag in self.events() if what == "start"], ["a", "cut-2", "late"])

    def test_a_kept_place_nobody_comes_back_to_holds_nobody_up(self):
        a = self.step("a", seconds=1.0, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        gone = self.step("gone", seconds=0.3, need="docker_mb=6000", extra=["--wait", "0.3"])
        gone.communicate(timeout=30)
        b = self.step("b", seconds=0.3, need="docker_mb=6000")
        self.finish([a, b])
        s = self.spans()
        self.assertLess(s["b"][0] - s["a"][1], 1.5)  # b did not wait for the one that never came back


class FairnessTest(Machine):
    settings = {"starve_sec": 1.5}

    def test_a_large_step_is_overtaken_only_for_so_long(self):
        first = self.step("first", seconds=1.0)
        self.wait_for(lambda: self.started("first"), what="first to start")
        big = self.step("big", seconds=0.3, need="docker_mb=8000")  # needs the machine nearly to itself
        self.wait_for(lambda: len(self.waiting()) == 1, what="big to wait")
        t_big = time.time()
        small = []
        for i in range(14):  # small ones keep coming for longer than big is willing to be overtaken
            small.append(self.step("small%d" % i, seconds=0.5))
            time.sleep(0.25)
        self.finish([first, big] + small, timeout=180)
        s = self.spans()
        waited = s["big"][0] - t_big
        self.assertLess(waited, 6.0, "big waited %.1f s" % waited)
        self.assertTrue([t for t in s if t.startswith("small") and s[t][0] < s["big"][0]],
                        "small steps may use the room while big cannot")
        self.assertTrue([t for t in s if t.startswith("small") and s[t][0] >= s["big"][0]],
                        "and the ones that came once big had waited long enough went after it")
        for t in s:  # while big ran, nothing ran beside it that would not fit with it
            if t != "big" and s[t][0] < s["big"][1] and s[t][1] > s["big"][0]:
                self.fail("%s ran beside big" % t)


class LearningTest(Machine):
    def test_what_a_step_used_is_what_the_next_one_is_counted_at(self):
        r1 = self.finish([self.step("g1", seconds=0.9, need=None, use="docker_mb=2000,cpu=2")])["g1"]
        self.assertIn("first guess", r1["need_source"])
        self.assertEqual(r1["need"], {"share": {"docker_mb": 0.25, "host_mb": 0.10, "cpu": 0.25}})  # shares, not sizes
        self.assertAlmostEqual(r1["peak"]["docker_mb"], 2000, delta=1)
        r2 = self.finish([self.step("g2", seconds=0.5, need=None, use="docker_mb=2000,cpu=2")])["g2"]
        self.assertEqual(r2["need_source"], "measured (1)")
        self.assertAlmostEqual(r2["need"]["docker_mb"], 2000 * 1.15, delta=1)
        self.assertAlmostEqual(r2["need"]["cpu"], 2 * 1.15, delta=0.01)
        other = self.finish([self.step("x1", seconds=0.3, need=None, use="docker_mb=10", kind="setup")])["x1"]
        self.assertIn("first guess", other["need_source"])  # another kind of step is another matter

    def test_the_same_shares_on_another_machine(self):
        self.set_machine(cpus=32, host={"total_mb": 128000}, docker={"total_mb": 128000})  # Linux: Docker has no VM of its own
        self.finish([self.step("s%d" % i, seconds=0.8, need="docker_mb=8000,cpu=3") for i in range(12)])
        # 32 CPUs allow 10 steps of 3; memory allows 13 (108800 MB after its reserve, 8000 each): the CPUs decide
        self.assertEqual(self.most_at_once(), 10)

    def test_a_small_machine_gives_fewer_places_and_no_setting_is_touched(self):
        self.set_machine(cpus=2, host={"total_mb": 4000}, docker={"total_mb": 4000})
        self.finish([self.step("s%d" % i, seconds=0.6, need=None, use="docker_mb=300,cpu=0.3") for i in range(4)])
        # Not measured yet, each counts as a quarter of Docker and a tenth of the machine: 1240 of the 3400 MB this
        # machine can give. Two fit, a third does not; on the machine of the test above the same shares let more in.
        self.assertEqual(self.most_at_once(), 2)


class ManyAtOnceTest(Machine):
    def test_the_shared_file_survives_many_processes(self):
        procs = [self.step("m%d" % i, seconds=0.05, need="docker_mb=%d" % (500 + 100 * (i % 7))) for i in range(40)]
        res = self.finish(procs, timeout=300)
        self.assertEqual(len(self.spans()), 40)
        self.assertTrue(all(r.get("exit") == 0 for r in res.values()), res)
        st = self.state()
        self.assertEqual((st["leases"], st["queue"]), ({}, []))
        need = {p.tag: 500 + 100 * (i % 7) for i, p in enumerate(procs)}
        load_, peak = 0, 0
        for _t, what, tag in self.events():  # at no moment was more promised than the machine holds
            load_ += need[tag] if what == "start" else -need[tag]
            peak = max(peak, load_)
        self.assertLessEqual(peak, 9000)


class CapacityTest(Machine):
    def fx(self, *argv):
        p = subprocess.run([sys.executable, FX_PATH] + list(argv), env=self.env, cwd=self.tmp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_it_says_what_runs_what_waits_and_why(self):
        a = self.step("a", seconds=3.0, need="docker_mb=6000")
        self.wait_for(lambda: self.started("a"), what="a to start")
        b = self.step("b", seconds=0.2, need="docker_mb=6000")
        self.wait_for(lambda: len(self.waiting()) == 1, what="b to wait")
        code, res = self.fx("capacity")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["room"]["docker_mb"], {"holds": 9000.0, "others_use": 0.0, "factory_steps": 6000.0, "free": 3000.0})
        self.assertEqual([(r["name"], r["counted_at"]["docker_mb"]) for r in res["running"]], [("a", 6000.0)])
        self.assertEqual([(w["name"], w["ahead"]) for w in res["waiting"]], [("b", 0)])
        self.assertIn("Docker's memory", res["waiting"][0]["why"])
        self.finish([a, b])

    def test_a_machines_own_settings_are_written_by_fx(self):
        code, res = self.fx("capacity", "--set", "pressure=strict", "--set", "first_guess.cpu=0.5")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["settings"], {"pressure": "strict", "first_guess": {"docker_mb": 0.25, "host_mb": 0.10, "cpu": 0.5}})
        self.assertEqual(load(res["settings_file"]), {"pressure": "strict", "first_guess": {"cpu": 0.5}})
        code, res = self.fx("capacity", "--reset", "pressure")
        self.assertEqual(res["settings"], {"first_guess": {"docker_mb": 0.25, "host_mb": 0.10, "cpu": 0.5}})
        for bad in ("pressure=sometimes", "reserve_host=2", "gates_at_once=1", "safety"):
            code, res = self.fx("capacity", "--set", bad)
            self.assertEqual(code, 3, res)

    def test_it_says_so_when_the_machine_does_not_tell_its_memory(self):
        self.set_machine(cpus=8, host={"total_mb": 0})
        code, res = self.fx("capacity")
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(res["room"]), ["cpu"])
        self.assertTrue(any("does not tell how much memory" in n for n in res["notes"]), res["notes"])


class MergeTest(unittest.TestCase):
    """job.json when two commands saved it from the same start (Ctx.save puts both together)."""

    @staticmethod
    def a_run(agent, i):
        return {"agent": agent, "round": 1, "started": "t%d" % i, "ended": None}

    def test_a_list_neither_had_at_the_start_keeps_what_both_put_in_it(self):
        """The first prompts of a job: four asked in one message, and job.json had no runs yet."""
        a, b = self.a_run("code", 1), self.a_run("security", 2)
        self.assertEqual(fx.merge3({"id": "J-1"}, {"id": "J-1", "runs": [a]}, {"id": "J-1", "runs": [b]})["runs"], [b, a])
        self.assertEqual(fx.merge3({"x": None}, {"x": [1]}, {"x": [2]}), {"x": [2, 1]})
        self.assertEqual(fx.merge3({}, {"lanes": {"ui": "L1"}}, {"lanes": {"api": "L2"}}), {"lanes": {"ui": "L1", "api": "L2"}})

    def test_a_list_kept_to_a_length_keeps_both_additions(self):
        base = {"runs": [self.a_run("old", i) for i in range(300)]}
        mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
        mine["runs"].append(self.a_run("code", 1000))  # this command: save cuts the list after putting both together
        theirs["runs"].append(self.a_run("security", 1001))
        del theirs["runs"][:-300]  # the other command saved first, and cut
        runs = fx.merge3(base, mine, theirs)["runs"]
        self.assertEqual([r["agent"] for r in runs[-2:]], ["security", "code"])
        self.assertEqual(runs[:299], base["runs"][1:])

    def test_a_change_to_an_entry_lands_on_that_entry_after_the_list_moved_up(self):
        base = {"runs": [self.a_run("old", i) for i in range(300)]}
        mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
        mine["runs"][299]["ended"] = "now"  # this command closed the latest run
        theirs["runs"] += [self.a_run("security", 1001), self.a_run("ux", 1002)]
        del theirs["runs"][:-300]
        runs = fx.merge3(base, mine, theirs)["runs"]
        self.assertEqual([(r["started"], r["ended"]) for r in runs[-3:]], [("t299", "now"), ("t1001", None), ("t1002", None)])
        self.assertEqual(len(runs), 300)

    def test_what_only_one_side_changed_is_kept_and_the_one_saving_wins_a_tie(self):
        base = {"stage": "build", "round": 1, "note": "x", "gone": 1}
        mine = {"stage": "review", "round": 1, "note": "mine"}
        theirs = {"stage": "build", "round": 2, "note": "theirs", "gone": 1, "new": True}
        self.assertEqual(fx.merge3(base, mine, theirs), {"stage": "review", "round": 2, "note": "mine", "new": True})


class ReadingTest(unittest.TestCase):
    """What a machine cannot be asked holds no step back on it."""

    def test_a_mac_whose_tools_do_not_answer(self):
        with unittest.mock.patch.object(fx.sys, "platform", "darwin"), \
                unittest.mock.patch.object(fx, "try_out", return_value=None):
            host = fx.meter_host()
        self.assertEqual(host["total_mb"], fx.mem_total_mb())  # the C library still knows the size
        self.assertEqual(host["avail_mb"], host["total_mb"])  # nothing said about what is in use: then none of it counts
        self.assertEqual((host["pressure"], host["pressure_source"]), (0, "no pressure counter here"))

    def test_memory_of_unknown_size_is_not_a_pool(self):
        reading = {"cpus": 8, "host": {"total_mb": 0.0, "avail_mb": 0.0}, "docker": None}
        self.assertEqual(sorted(fx.pool_capacity(fx.MACHINE_DEFAULTS, reading)), ["cpu"])
        reading["host"]["total_mb"] = 16000.0
        self.assertEqual(sorted(fx.pool_capacity(fx.MACHINE_DEFAULTS, reading)), ["cpu", "host_mb"])


# --------------------------------------------------------------------------
# tokens: what an agent really used, and when a builder is continued

class TokenUseTest(BoxCase):
    @staticmethod
    def call(mid, inp, out, read, wrote):
        return {"type": "assistant", "message": {"id": mid, "model": "claude-sonnet", "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": wrote}}}

    def transcript(self, agent_id, calls):
        cfg = os.path.join(self.tmp, "claude-config")
        path = os.path.join(cfg, "projects", "-some-project", "session-1", "subagents", "agent-%s.jsonl" % agent_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps(c) + "\n")
        return dict(self.box.env, CLAUDE_CONFIG_DIR=cfg)

    def test_usage_is_read_from_the_transcript_and_a_continued_agent_counts_only_its_new_calls(self):
        f = self.box.factory("tok")
        job = self.box.new(f, "TOK-1")["job"]
        self.box.to_build(f, job)
        # one call written as two lines (one per content block) counts once
        env = self.transcript("abc123", [self.call("m1", 10, 100, 1000, 50), self.call("m1", 10, 100, 1000, 50),
                                         {"type": "user", "message": {"content": "x"}},
                                         self.call("m2", 1, 20, 3000, 5)])
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "builder", env=env)[0], 0)
        code, res = self.box.fx(f, "usage", job, "--agent", "builder", "--tokens", "4100", "--ms", "9",
                                "--agent-id", "abc123", env=env)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["usage"], {"input": 11, "output": 120, "cache_read": 4000, "cache_write": 55})
        self.assertEqual((res["tokens"], res["context"]), (4186, 4100))
        # continued (same agent, same transcript): the second run is only what came after the first
        env = self.transcript("abc123", [self.call("m3", 2, 30, 5000, 0)])
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "builder", env=env)[0], 0)
        code, res = self.box.fx(f, "usage", job, "--agent", "builder", "--tokens", "5032", "--agent-id", "abc123",
                                env=env)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["usage"], {"input": 2, "output": 30, "cache_read": 5000, "cache_write": 0})
        runs = [r for r in self.box.job(f, job)["runs"] if r["agent"] == "builder"]
        self.assertEqual([r["tokens"] for r in runs], [4186, 5032])
        self.assertEqual([r["context"] for r in runs], [4100, 5032])

    def test_without_a_transcript_the_reported_number_stays_and_says_what_it_is(self):
        f = self.box.factory("tok2")
        job = self.box.new(f, "TOK-2")["job"]
        self.box.to_build(f, job)
        env = dict(self.box.env, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "empty-config"))
        self.assertEqual(self.box.fx(f, "ctx", job, "--agent", "builder", env=env)[0], 0)
        for agent_id in ("nothere", "../../etc/passwd"):
            code, res = self.box.fx(f, "usage", job, "--agent", "builder", "--tokens", "700", "--agent-id", agent_id,
                                    env=env)
            self.assertEqual(code, 0, res)
            self.assertEqual((res["tokens"], res["usage"], res["measured"]), (700, None, "reported context only"))

    def builder_ctx(self, f, job, context):
        self.box.edit(f, job, stage="build", round=1, round_start=0, max_rounds=3, build_reason="feedback",
                      builder_agent_id="agent-1", builder_agent_model="opus",
                      runs=[{"agent": "builder", "name": "builder", "wp": None, "cand": None, "round": 0,
                             "model": "opus", "status": "done", "started": "2026-01-01T00:00:00+00:00",
                             "ended": "2026-01-01T00:10:00+00:00", "result": [], "tokens": 9, "context": context}])
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder")
        self.assertEqual(code, 0, res)
        return res

    def test_a_builder_is_continued_only_while_its_context_is_small(self):
        f = self.box.factory("res")
        job = self.box.new(f, "RES-1")["job"]
        self.box.to_build(f, job)
        small = self.builder_ctx(f, job, 40000)
        self.assertEqual(small.get("resume_agent_id"), "agent-1")
        self.assertFalse(small["task"].startswith("You are a new builder"), small["task"])
        large = self.builder_ctx(f, job, 400000)
        self.assertNotIn("resume_agent_id", large)
        self.assertTrue(large["task"].startswith("You are a new builder"), large["task"])
        self.assertIn("build.md", large["task"])
        self.assertIn("Round 1 feedback", large["task"])

    def test_the_spec_written_again_after_answers_starts_from_its_draft_on_sonnet(self):
        f = self.box.factory("spa")
        job = self.box.new(f, "SPA-1")["job"]
        self.box.edit(f, job, stage="spec", risk="high", models={"intake": "sonnet", "spec": "opus"})
        code, first = self.box.fx(f, "ctx", job, "--agent", "spec")
        self.assertEqual(code, 0, first)
        self.assertEqual(first["model"], "opus")
        jd = os.path.join(f, "jobs", job)
        write(os.path.join(jd, "spec-asked-1.md"), "SPEC: NEEDS-INFO\n")
        write(os.path.join(jd, "spec-asked-2.md"), "SPEC: NEEDS-INFO\n")
        write(os.path.join(jd, "qa-log.md"), "# Questions and answers\n")
        code, again = self.box.fx(f, "ctx", job, "--agent", "spec")
        self.assertEqual(code, 0, again)
        self.assertEqual(again["model"], "sonnet")
        self.assertIn("spec-asked-2.md", again["task"])

    def test_the_cost_of_a_run_prices_each_kind(self):
        use = {"input": 10 ** 6, "output": 10 ** 6, "cache_read": 10 ** 6, "cache_write": 10 ** 6}
        self.assertAlmostEqual(fx.run_cost({"usage": use, "tokens": 4 * 10 ** 6}, 5), 5 + 25 + 0.5 + 6.25)
        self.assertAlmostEqual(fx.run_cost({"usage": use}, {"input": 5, "output": 20}), 5 + 20 + 0.5 + 6.25)
        self.assertAlmostEqual(fx.run_cost({"tokens": 2 * 10 ** 6}, 5), 10)  # recorded before: priced as input
        self.assertIsNone(fx.run_cost({"usage": use}, None))
        self.assertEqual(fx.board_cost([{"model": "opus", "tokens": 4 * 10 ** 6, "usage": use}], {"opus": 5}),
                         (4 * 10 ** 6, 36.75))


if __name__ == "__main__":
    unittest.main(verbosity=2)
