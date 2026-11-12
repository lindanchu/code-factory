"""Tests for one issue across several repositories (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

Each repository is real: a bare origin, its own factory repository and real clones. Only gh, glab and the scanners are
stubs: the gh stub records every call and the description it was given, so "the pull requests point to each other" is
checked on what would have reached the host.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_jira import EMAIL, TOKEN, FakeJira, KEY as JIRA_KEY, an_issue
from test_security import STUB, SECRET_ENV, fx, load_json, sh, slurp, write

KEY = "PROJ-9"
JIRA = "https://example.atlassian.net"
ISSUE = "# %s: Reset password\n\n- Link: %s/browse/%s\n\n## Description\nBE, FE and the app all change.\n" % (KEY, JIRA, KEY)
SCOPE = """SCOPE: READY

## Repo: be
Add POST /api/password-reset. Returns 202.

## Repo: fe
Add the reset form, which calls POST /api/password-reset.

## Shared contract
POST /api/password-reset takes {"email": string} and answers 202.

## Left out
Nothing.
"""
GH_STUB = r'''#!%(python)s -I
import json, os, subprocess, sys
args = sys.argv[1:]
rec = {"argv": args, "cwd": os.getcwd()}
if "--body-file" in args:
    rec["body"] = open(args[args.index("--body-file") + 1]).read()
if args[:2] == ["pr", "edit"] and os.environ.get("GH_FAIL_EDIT"):
    print("gh: edit refused")
    sys.exit(1)
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
if args[:2] == ["pr", "create"]:
    origin = subprocess.run(["git", "remote", "get-url", "origin"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print("https://github.com/acme/%%s/pull/7" %% os.path.basename(origin).replace(".git", ""))
elif args[:2] == ["pr", "view"]:
    sys.exit(1)
'''
GLAB_STUB = r'''#!%(python)s -I
import json, os, sys
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps({"tool": "glab", "argv": sys.argv[1:]}) + "\n")
'''
CHECKS = "#!/usr/bin/env bash\nexit 0\n"
GITIGNORE = "jobs/\nbaseline.json\nlocal/\n.claude/settings.local.json\n"


class MultiCase(unittest.TestCase):
    repos = (("be", 10), ("fe", 20))

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-multi-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        gitcfg = os.path.join(self.tmp, "gitconfig")
        write(gitcfg, "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for tool in ("gitleaks", "semgrep", "osv-scanner", "docker"):
            self.stub(tool, STUB % {"python": sys.executable, "secret_names": sorted(SECRET_ENV)})
        self.stub("gh", GH_STUB % {"python": sys.executable})
        self.stub("glab", GLAB_STUB % {"python": sys.executable})
        for real in ("git", "bash", "tar"):
            os.symlink(shutil.which(real), os.path.join(self.bin, real))
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.workspace = os.path.join(self.tmp, "machine", "workspace.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JIRA_", "GH_", "GITHUB_", "GITLAB_"))}
        env.update(GIT_CONFIG_GLOBAL=gitcfg, GIT_CONFIG_NOSYSTEM="1", FACTORY_STATE_DIR=os.path.join(self.tmp, "state"),
                   FACTORY_TOOLS_DIR=os.path.join(self.tmp, "tools"), FACTORY_WORKSPACE=self.workspace,
                   PATH=self.bin + os.pathsep + "/usr/bin:/bin", STUB_LOG=os.path.join(self.tmp, "stub.log"),
                   GH_LOG=self.gh_log, HOME=self.tmp)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.factory, self.origin = {}, {}
        for name, order in self.repos:
            self.make_repo(name, order)

    def stub(self, name, text):
        path = os.path.join(self.bin, name)
        write(path, text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def make_repo(self, name, order, register=True):
        """A project with main and develop, and its own factory repository ready for jobs."""
        seed = os.path.join(self.tmp, "seed-" + name)
        os.makedirs(seed)
        sh(["git", "init", "-q", "-b", "main"], seed)
        write(os.path.join(seed, "README.md"), "%s\n" % name)
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "base"], seed)
        sh(["git", "checkout", "-q", "-b", "develop"], seed)
        write(os.path.join(seed, "only-on-develop.txt"), "develop\n")
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "develop"], seed)
        sh(["git", "checkout", "-q", "main"], seed)
        origin = os.path.join(self.tmp, name + ".git")
        sh(["git", "clone", "-q", "--bare", seed, origin], self.tmp)
        factory = os.path.join(self.tmp, name + "-factory")
        os.makedirs(factory)
        sh(["git", "init", "-q", "-b", "main"], factory)
        self.origin[name], self.factory[name] = origin, factory
        code, res = self.fx("init", "--project", origin, repo=factory)
        self.assertEqual(code, 0, res)
        conf = load_json(os.path.join(factory, "factory.json"))
        conf["project"]["name"] = name
        conf["jira"] = {"base_url": JIRA, "transitions": {"start": "In Progress", "review": "In Review"}}
        conf["pr"] = {"create": True, "provider": "github", "labels": []}
        conf["security"] = {"required": [], "scanners": {"zap": False, "headers": False}}
        conf["learning"] = {"ask_after": "never"}
        conf["language"] = {"be": "English", "fe": "Vietnamese"}.get(name, "English")
        write(os.path.join(factory, "factory.json"), json.dumps(conf, indent=1))
        write(os.path.join(factory, "checks.sh"), CHECKS)
        for fname in os.listdir(os.path.join(factory, "knowledge")):
            path = os.path.join(factory, "knowledge", fname)
            write(path, slurp(path).replace("FACTORY_TODO", "done"))
        write(os.path.join(factory, "knowledge", "stack.md"), "# %s stack\n" % name)
        write(os.path.join(factory, ".gitignore"), GITIGNORE)
        sh(["git", "add", "-A"], factory)
        sh(["git", "commit", "-q", "-m", "factory"], factory)
        if register:
            code, res = self.fx("workspace", "add", "--name", name, "--order", str(order), repo=factory)
            self.assertEqual(code, 0, res)

    def fx(self, *argv, repo=None, stdin=None):
        """Run fx in-process; returns (exit code, parsed JSON output)."""
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin if stdin is not None else "")):
            try:
                fx.main(["--repo", repo or self.factory["be"]] + list(argv))
            except SystemExit as e:
                code = e.code
        os.chdir(self.cwd)
        return code, json.loads(buf.getvalue())

    def ok(self, *argv, repo=None, stdin=None):
        code, res = self.fx(*argv, repo=repo, stdin=stdin)
        self.assertEqual(code, 0, res)
        return res

    def run_dir(self, key=KEY):
        return os.path.join(os.path.dirname(self.workspace), "features", key)

    def issue_file(self):
        path = os.path.join(self.tmp, "issue-%s.md" % KEY)
        write(path, ISSUE)
        return path

    def scope(self, text=SCOPE):
        self.ok("feature", "ctx", KEY, "--issue-file", self.issue_file())
        write(os.path.join(self.run_dir(), "scope.md"), text)
        return self.ok("feature", "scope", KEY)

    def start(self, bases=None):
        steps = [{"repo": r, "base": b} for r, b in (bases or {"be": "develop", "fe": "main"}).items()]
        return self.ok("feature", "start", KEY, stdin=json.dumps(steps))

    def new(self, name, key=KEY):
        return self.ok("new", key, "--feature", KEY, "--issue-file", self.issue_file(), repo=self.factory[name])

    def job_path(self, name, job=KEY):
        return os.path.join(self.factory[name], "jobs", job, "job.json")

    def job(self, name, job=KEY):
        return load_json(self.job_path(name, job))

    def to_pr(self, name, job=KEY):
        """The job has passed everything: only the pull request is left."""
        j = self.job(name, job)
        wt = j["worktree"]
        write(os.path.join(wt, "work-%s.txt" % name), "done\n")
        sh(["git", "add", "-A"], wt)
        sh(["git", "commit", "-q", "-m", "%s: work" % j["issue_key"]], wt)
        j.update(stage="pr", decision="APPROVE", gate="PASS", risk="low")
        write(self.job_path(name, job), json.dumps(j, indent=1))

    def pr(self, name, job=KEY, target="main"):
        self.to_pr(name, job)
        return self.ok("pr", job, "--target", target, repo=self.factory[name])

    def gh_calls(self):
        if not os.path.exists(self.gh_log):
            return []
        return [json.loads(line) for line in slurp(self.gh_log).splitlines()]

    def fit_pass(self, text="FIT: PASS\n\n## Checked\nThe form calls the route the API added.\n"):
        self.ok("feature", "fit", KEY)
        write(os.path.join(self.run_dir(), "fit.md"), text)
        return self.ok("feature", "fit-result", KEY)

    def assert_no_machine_path(self, text):
        self.assertNotIn(self.tmp, text)
        self.assertNotIn("/factory", text.replace("software factory", ""))


# --------------------------------------------------------------------------
# where each repository is: this machine's own memory

class WorkspaceTest(MultiCase):
    repos = ()

    def setUp(self):
        super().setUp()
        self.make_repo("be", 10, register=False)
        self.make_repo("fe", 20, register=False)
        self.here = self.factory["be"]

    def add(self, *argv, repo=None):
        return self.fx("workspace", "add", *argv, repo=repo or self.here)

    def test_a_factory_folder_is_remembered(self):
        code, res = self.add("--path", self.factory["fe"], "--name", "fe", "--order", "20")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["added"]["path"], self.factory["fe"])
        self.assertEqual(load_json(self.workspace)["repos"][0]["name"], "fe")

    def test_the_project_clone_leads_to_its_factory(self):
        self.ok("board", repo=self.factory["fe"])  # this machine has seen the factory
        clone = os.path.join(self.tmp, "my-code", "fe")
        sh(["git", "clone", "-q", self.origin["fe"], clone], self.tmp)
        code, res = self.add("--path", clone, "--name", "fe")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["added"]["path"], self.factory["fe"])
        self.assertIn("own clone", res["note"])

    def test_a_project_clone_without_a_factory_is_refused_with_the_way_out(self):
        other = os.path.join(self.tmp, "other.git")
        sh(["git", "init", "-q", "--bare", other], self.tmp)
        clone = os.path.join(self.tmp, "my-code", "other")
        sh(["git", "clone", "-q", other, clone], self.tmp)
        code, res = self.add("--path", clone)
        self.assertEqual(code, 3)
        self.assertIn("/factory init", res["error"])

    def test_a_folder_that_is_not_here_is_asked_again(self):
        code, res = self.add("--path", os.path.join(self.tmp, "on-another-machine"))
        self.assertEqual(code, 3)
        self.assertIn("ask the user", res["error"])

    def test_a_plain_folder_is_refused(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        code, res = self.add("--path", plain)
        self.assertEqual(code, 3)
        self.assertIn("neither", res["error"])

    def test_a_moved_folder_only_changes_the_folder(self):
        self.ok("workspace", "add", "--path", self.factory["fe"], "--name", "fe", "--order", "20", "--components", "web",
                repo=self.here)
        moved = os.path.join(self.tmp, "moved-fe-factory")
        shutil.move(self.factory["fe"], moved)
        res = self.ok("workspace", "add", "--path", moved, "--name", "fe", repo=self.here)
        self.assertEqual(res["added"]["path"], moved)
        self.assertEqual(res["added"]["order"], 20)
        self.assertEqual(res["added"]["components"], ["web"])
        self.assertEqual(len(load_json(self.workspace)["repos"]), 1)

    def test_what_the_team_shares_comes_from_factory_json(self):
        """A second machine answers only the folder."""
        path = os.path.join(self.factory["fe"], "factory.json")
        conf = load_json(path)
        conf["workspace"] = {"name": "web", "order": 25, "components": ["ui"], "labels": ["fe"], "project": "PROJ"}
        write(path, json.dumps(conf, indent=1))
        res = self.ok("workspace", "add", "--path", self.factory["fe"], repo=self.here)
        self.assertEqual(res["added"]["name"], "web")
        self.assertEqual(res["added"]["order"], 25)
        self.assertEqual(res["added"]["components"], ["ui"])
        self.assertEqual(res["added"]["project"], "PROJ")
        again = self.ok("workspace", "add", "--path", self.factory["fe"], "--order", "40", repo=self.here)
        self.assertEqual(again["added"]["order"], 40, "a flag wins, for this machine")

    def test_a_name_of_another_project_is_refused(self):
        self.ok("workspace", "add", "--path", self.factory["be"], "--name", "app", repo=self.here)
        code, res = self.add("--path", self.factory["fe"], "--name", "app")
        self.assertEqual(code, 3)
        self.assertIn("already", res["error"])

    def test_a_name_that_could_be_a_path_is_refused(self):
        code, res = self.add("--name", "../evil")
        self.assertEqual(code, 3)
        self.assertIn("letters", res["error"])

    def test_list_says_what_is_gone_and_what_else_this_machine_knows(self):
        self.ok("board", repo=self.factory["fe"])
        gone = os.path.join(self.tmp, "gone-factory")
        shutil.copytree(self.factory["be"], gone)
        self.ok("workspace", "add", "--path", gone, "--name", "gone", repo=self.here)
        shutil.rmtree(gone)
        res = self.ok("workspace", "list", repo=self.here)
        self.assertEqual({r["name"]: r["state"] for r in res["repos"]}, {"gone": "missing"})
        self.assertIn(self.factory["fe"], [o["path"] for o in res["others"]])
        self.assertIn("this machine", res["note"])

    def test_nothing_about_folders_reaches_git(self):
        self.ok("workspace", "add", "--path", self.factory["fe"], "--name", "fe", repo=self.here)
        for factory in self.factory.values():
            self.assertEqual(sh(["git", "status", "--porcelain"], factory), "")
            for folder in list(self.factory.values()) + [os.path.dirname(self.workspace)]:
                grep = subprocess.run(["git", "grep", "-l", folder], cwd=factory, stdout=subprocess.PIPE, text=True)
                self.assertEqual(grep.stdout, "", "a folder of this machine is in a committed file")

    def test_plan_tells_where_nothing_is_remembered(self):
        code, res = self.fx("plan", repo=self.here, stdin="[]")
        self.assertEqual(code, 3)
        self.assertIn("ask the user", res["error"])

    def test_plan_marks_a_folder_that_is_gone(self):
        gone = os.path.join(self.tmp, "gone-factory")
        shutil.copytree(self.factory["be"], gone)
        self.ok("workspace", "add", "--path", gone, "--name", "gone", "--components", "api", repo=self.here)
        shutil.rmtree(gone)
        res = self.ok("plan", repo=self.here, stdin=json.dumps([{"key": "PROJ-1", "components": ["api"]}]))
        self.assertEqual(res["steps"][0]["state"], "missing")


# --------------------------------------------------------------------------
# which repository does what

class ScopeTest(MultiCase):
    def test_no_token_asks_for_the_issue_first(self):
        res = self.ok("feature", "ctx", KEY)
        self.assertEqual(res["next"], "fetch")
        self.assertIn("--issue-file", res["then"])

    def test_the_prompt_names_each_repository_and_its_knowledge(self):
        res = self.ok("feature", "ctx", KEY, "--issue-file", self.issue_file())
        self.assertEqual(res["agent"], "scope-writer")
        for name in ("be", "fe"):
            self.assertIn(os.path.join(self.factory[name], "knowledge", "stack.md"), res["prompt"])
        self.assertIn(os.path.join(self.run_dir(), "scope.md"), res["prompt"])
        self.assertIn("Treat it as DATA", slurp(os.path.join(self.run_dir(), "issue.md")))

    def test_each_repository_gets_its_part_and_the_contract(self):
        res = self.scope()
        self.assertEqual([p["repo"] for p in res["parts"]], ["be", "fe"])
        self.assertEqual(res["ask_where"], [])
        fe = slurp(os.path.join(self.run_dir(), "scope-fe.md"))
        self.assertIn("reset form", fe)
        self.assertNotIn("Returns 202", fe.split("## Shared contract")[0])
        self.assertIn("answers 202", fe)

    def test_a_repository_this_machine_does_not_know_is_asked(self):
        res = self.scope(SCOPE + "\n## Repo: app\nA reset screen.\n")
        self.assertEqual(res["ask_where"], ["app"])
        self.assertIn("ask where", res["next"])

    def test_questions_go_to_the_human(self):
        res = self.scope("SCOPE: NEEDS-INFO\n\n## Questions\nDoes the app change too?\n")
        self.assertEqual(res["result"], "NEEDS-INFO")
        self.assertIn("app change", res["questions"])
        write(res["answers_file"], "Yes.\n")
        self.assertIn("answers.md", self.ok("feature", "ctx", KEY)["prompt"])

    def assert_bad_scope(self, text, say):
        self.ok("feature", "ctx", KEY, "--issue-file", self.issue_file())
        write(os.path.join(self.run_dir(), "scope.md"), text)
        code, res = self.fx("feature", "scope", KEY)
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "scope-writer")
        self.assertIn(say, res["error"])

    def test_a_bad_result_is_sent_back(self):
        self.assert_bad_scope("All good\n", "not a valid")
        self.assert_bad_scope("SCOPE: READY\n\nNo sections.\n", "## Repo")
        self.assert_bad_scope("SCOPE: READY\n\n## Repo: be\n\n## Repo: fe\ntext\n", "empty: be")
        self.assert_bad_scope("SCOPE: READY\n\n## Repo: ../../etc\ntext\n", "short name")
        self.assert_bad_scope("SCOPE: READY\n\n## Repo: be\na\n\n## Repo: be\nb\n", "two sections")

    def test_no_scope_file_is_a_retry(self):
        self.ok("feature", "ctx", KEY, "--issue-file", self.issue_file())
        code, res = self.fx("feature", "scope", KEY)
        self.assertEqual(code, 6, res)


# --------------------------------------------------------------------------
# before any job: where, which branch, and whether every repository can run a job

class StartTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()

    def try_start(self, steps):
        return self.fx("feature", "start", KEY, stdin=json.dumps(steps))

    def assert_no_job_anywhere(self):
        for factory in self.factory.values():
            self.assertFalse(os.path.exists(os.path.join(factory, "jobs", KEY)))

    def test_a_branch_is_never_assumed(self):
        code, res = self.try_start([{"repo": "be", "base": "develop"}, {"repo": "fe"}])
        self.assertEqual(code, 3, res)
        self.assertEqual(res["ask_branch"], ["fe"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir(), "feature.json")))

    def test_a_branch_the_remote_does_not_have_is_asked_again(self):
        code, res = self.try_start([{"repo": "be", "base": "develop"}, {"repo": "fe", "base": "devel"}])
        self.assertEqual(code, 3, res)
        self.assertEqual(res["ask_branch"], ["fe"])
        self.assertIn("develop", res["error"], "it offers the close name")
        self.assert_no_job_anywhere()

    def test_an_unknown_repository_is_asked(self):
        code, res = self.try_start([{"repo": "app", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertEqual(res["ask_where"], ["app"])

    def test_a_folder_that_is_gone_is_asked_again(self):
        shutil.move(self.factory["fe"], self.factory["fe"] + "-moved")
        code, res = self.try_start([{"repo": "be", "base": "main"}, {"repo": "fe", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertEqual(res["ask_where"], ["fe"])

    def test_a_shared_issue_needs_a_part_for_each(self):
        os.remove(os.path.join(self.run_dir(), "scope-fe.md"))
        code, res = self.try_start([{"repo": "be", "base": "main"}, {"repo": "fe", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertIn("fe has no part", res["error"])

    def test_steps_follow_merge_order_with_their_own_branch(self):
        res = self.start({"fe": "main", "be": "develop"})
        self.assertEqual([(s["repo"], s["base"]) for s in res["steps"]], [("be", "develop"), ("fe", "main")])
        self.assertIn("--repo %s new %s --base develop --feature %s" % (self.factory["be"], KEY, KEY), res["steps"][0]["new"])

    def test_the_folders_go_in_this_machines_settings_only(self):
        self.start()
        settings = load_json(os.path.join(self.factory["be"], ".claude", "settings.local.json"))
        dirs = settings["permissions"]["additionalDirectories"]
        self.assertIn(self.factory["fe"], dirs)
        self.assertIn(self.factory["fe"] + "-work", dirs)
        self.assertEqual(sh(["git", "status", "--porcelain"], self.factory["be"]), "")

    def test_a_started_job_keeps_its_branch(self):
        self.start()
        self.new("be")
        code, res = self.try_start([{"repo": "be", "base": "main"}, {"repo": "fe", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertIn("already has job", res["error"])

    def test_a_repository_that_cannot_run_a_job_stops_the_run_before_it_starts(self):
        """Not after the first repositories are done."""
        write(os.path.join(self.factory["fe"], "checks.sh"), "# FACTORY_TODO\n")
        sh(["git", "add", "-A"], self.factory["fe"])
        sh(["git", "commit", "-q", "-m", "oops"], self.factory["fe"])
        code, res = self.try_start([{"repo": "be", "base": "main"}, {"repo": "fe", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertEqual(list(res["not_ready"]), ["fe"])
        self.assertIn("checks.sh filled in", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir(), "feature.json")))
        self.assert_no_job_anywhere()

    def test_uncommitted_settings_of_another_repository_stop_the_run(self):
        write(os.path.join(self.factory["fe"], "checks.sh"), CHECKS + "# changed\n")
        code, res = self.try_start([{"repo": "be", "base": "main"}, {"repo": "fe", "base": "main"}])
        self.assertEqual(code, 3, res)
        self.assertEqual([c["check"] for c in res["not_ready"]["fe"]], ["factory files committed"])

    def test_another_repositorys_session_settings_do_not_count(self):
        os.remove(os.path.join(self.factory["fe"], ".claude", "settings.local.json"))
        self.start()

    def test_same_ports_are_mentioned(self):
        res = self.start()
        self.assertTrue(any("ports.base" in n for n in res["notes"]), res["notes"])


# --------------------------------------------------------------------------
# each repository's job: its own branch, part, rules and checks

class JobTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()
        self.start()

    def test_the_job_starts_from_the_branch_the_user_chose(self):
        self.new("be")
        self.new("fe")
        self.assertEqual(self.job("be")["base_branch"], "develop")
        self.assertTrue(os.path.exists(os.path.join(self.job("be")["worktree"], "only-on-develop.txt")))
        self.assertEqual(self.job("fe")["base_branch"], "main")
        self.assertFalse(os.path.exists(os.path.join(self.job("fe")["worktree"], "only-on-develop.txt")))

    def test_without_a_token_the_second_command_keeps_branch_repo_and_run(self):
        res = self.ok("new", KEY, "--feature", KEY, repo=self.factory["be"])
        self.assertEqual(res["next"], "fetch")
        self.assertIn("--base develop", res["then"])
        self.assertIn("--feature %s" % KEY, res["then"])
        self.assertIn("--repo %s" % self.factory["be"], res["then"])

    def test_every_agent_of_a_shared_issue_is_told_its_part(self):
        self.new("fe")
        for agent in ("intake", "spec", "builder", "security", "code", "ux", "ui", "approver"):
            prompt = self.ok("ctx", KEY, "--agent", agent, repo=self.factory["fe"])["prompt"]
            self.assertIn(os.path.join(self.factory["fe"], "jobs", KEY, "scope.md"), prompt)
            self.assertIn("be", prompt.split("Other jobs do the rest (")[1].split(")")[0])
        scope = slurp(os.path.join(self.factory["fe"], "jobs", KEY, "scope.md"))
        self.assertIn("reset form", scope)

    def test_each_job_works_by_its_own_repositorys_rules(self):
        """The conversation runs in the factory of be; the job of fe follows fe."""
        self.new("fe")
        prompt = self.ok("ctx", KEY, "--agent", "builder", repo=self.factory["fe"])["prompt"]
        self.assertIn(os.path.join(self.factory["fe"], "knowledge", "stack.md"), prompt)
        self.assertNotIn(os.path.join(self.factory["be"], "knowledge"), prompt)
        self.assertIn(os.path.join(self.factory["fe"], "checks.sh"), prompt)
        self.assertIn("Human language: Vietnamese", prompt)
        self.assertIn("Factory repository (never edit it): %s" % self.factory["fe"], prompt)
        be = self.new("be")
        self.assertIn(self.factory["be"] + "-work", be["worktree"])
        self.assertIn(self.factory["fe"] + "-work", self.job("fe")["worktree"])

    def test_a_later_repository_builds_on_the_api_notes_of_an_earlier_one(self):
        self.new("be")
        write(os.path.join(self.factory["be"], "jobs", KEY, "api-notes.md"), "POST /api/password-reset\n")
        self.new("fe")
        ups = self.job("fe")["upstream"]
        self.assertEqual(len(ups), 1)
        self.assertEqual(slurp(ups[0]), "POST /api/password-reset\n")
        self.assertIn("Upstream API notes", self.ok("ctx", KEY, "--agent", "builder", repo=self.factory["fe"])["prompt"])

    def test_notes_never_flow_against_the_merge_order(self):
        self.new("fe")
        write(os.path.join(self.factory["fe"], "jobs", KEY, "api-notes.md"), "x\n")
        self.new("be")
        self.assertEqual(self.job("be")["upstream"], [])

    def test_a_factory_outside_the_run_is_refused(self):
        self.make_repo("app", 30)
        code, res = self.fx("new", KEY, "--feature", KEY, "--issue-file", self.issue_file(), repo=self.factory["app"])
        self.assertEqual(code, 3, res)
        self.assertIn("not a step", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.factory["app"], "jobs", KEY)))

    def test_intake_of_a_part_cannot_send_it_back_to_multi_repo(self):
        self.new("fe")
        write(os.path.join(self.factory["fe"], "jobs", KEY, "intake.md"), "INTAKE: MULTI-REPO\n\n## Reason\nx\n")
        code, res = self.fx("result", KEY, "intake", repo=self.factory["fe"])
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "intake")

    def test_status_reads_the_jobs_themselves(self):
        self.new("be")
        res = self.ok("feature", "status", KEY)
        self.assertEqual([(s["repo"], s["state"]) for s in res["steps"]], [("be", "running"), ("fe", "not-started")])
        self.assertEqual(res["remaining"], ["be", "fe"])
        self.assertEqual(self.ok("status", repo=self.factory["be"])["jobs"][0]["feature"], KEY)


class IntakeDetectsTest(MultiCase):
    """/factory PROJ-9 in one repository: intake finds it spans others."""

    def setUp(self):
        super().setUp()
        self.ok("new", KEY, "--issue-file", self.issue_file(), "--base", "main", repo=self.factory["be"])
        write(os.path.join(self.factory["be"], "jobs", KEY, "intake.md"),
              "INTAKE: MULTI-REPO\nRISK: low\nSURFACES: api\n\n## Reason\nThe web form is in another repository.\n")

    def test_nothing_reaches_the_issue_and_the_run_is_offered(self):
        res = self.ok("result", KEY, "intake", repo=self.factory["be"])
        self.assertEqual(res["next"], "feature")
        self.assertEqual(res["feature"], KEY)
        self.assertIn("web form", res["reason"])
        job = self.job("be")
        self.assertEqual(job["stage"], "rejected")
        self.assertEqual(job.get("jira_todo", []), [], "no refusal is posted")
        self.assertFalse(os.path.exists(job["worktree"]))

    def test_the_run_then_takes_the_issue_over_without_asking_again(self):
        self.ok("result", KEY, "intake", repo=self.factory["be"])
        self.scope()
        self.start()
        res = self.new("be")
        self.assertEqual(res["job"], KEY + "-r2")
        self.assertEqual(self.ok("feature", "status", KEY)["steps"][0]["job"], KEY + "-r2")

    def test_a_job_outside_a_run_still_says_it_exists(self):
        self.ok("result", KEY, "intake", repo=self.factory["be"])
        code, res = self.fx("new", KEY, "--issue-file", self.issue_file(), repo=self.factory["be"])
        self.assertEqual(code, 2, res)

    def test_a_local_task_has_no_run_to_go_to(self):
        path = os.path.join(self.tmp, "task.md")
        write(path, "Do it\n")
        job = self.ok("new", "--local", "A task", "--issue-file", path, repo=self.factory["fe"])["job"]
        write(os.path.join(self.factory["fe"], "jobs", job, "intake.md"), "INTAKE: MULTI-REPO\n\n## Reason\nOther repo.\n")
        res = self.ok("result", job, "intake", repo=self.factory["fe"])
        self.assertEqual(res["next"], "reject")


class NoRunTest(MultiCase):
    """A job outside a run behaves as it always did."""

    def setUp(self):
        super().setUp()
        self.ok("new", KEY, "--issue-file", self.issue_file(), "--base", "main", repo=self.factory["be"])

    def test_without_a_token_the_branch_is_kept_for_the_second_command(self):
        res = self.ok("new", "PROJ-10", "--base", "develop", repo=self.factory["be"])
        self.assertIn("--base develop", res["then"])
        self.assertNotIn("--feature", res["then"])

    def test_the_prompt_has_no_part(self):
        self.assertNotIn("scope.md", self.ok("ctx", KEY, "--agent", "builder", repo=self.factory["be"])["prompt"])

    def test_the_pull_request_moves_the_issue_at_once(self):
        res = self.pr("be")
        self.assertIn("queued", res["jira"]["transition"])
        self.assertIsNone(res["feature"])
        self.assertNotIn("across repositories", slurp(os.path.join(self.factory["be"], "jobs", KEY, "pr-body.md")))
        comment = [i for i in self.job("be")["jira_todo"] if i["action"] == "comment"][-1]
        self.assertFalse(comment["markdown"].startswith("**[be]**"))
        self.assertFalse(os.path.exists(self.run_dir()))


# --------------------------------------------------------------------------
# pull requests that point to each other, and an issue that moves once

class PullRequestsTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()
        self.start()
        self.new("be")
        self.new("fe")

    def transitions(self, name):
        return [i for i in self.job(name).get("jira_todo", []) if i["action"] == "transition" and i["to"] == "In Review"]

    def comments(self, name):
        return [i["markdown"] for i in self.job(name).get("jira_todo", []) if i["action"] == "comment"]

    def give_a_token(self):
        """jira.env in each factory's keys folder: with no jira.mode chosen, fx calls Jira itself from now on."""
        for name in ("be", "fe"):
            with fx.factory_ctx(self.factory[name]) as c:
                path = fx.jira_env_path(c)
            write(path, "JIRA_EMAIL=a@example.com\nJIRA_API_TOKEN=t\n")
            os.chmod(path, 0o600)

    def test_the_first_pull_request_says_what_comes_after_it(self):
        res = self.pr("be")
        self.assertEqual(res["feature"]["remaining"], ["fe"])
        self.assertIn("skipped", res["jira"]["transition"])
        self.assertEqual(self.transitions("be"), [])
        body = slurp(os.path.join(self.factory["be"], "jobs", KEY, "pr-body.md"))
        self.assertIn("Merge them in this order", body)
        self.assertIn("| 1 | be | %s | this pull request |" % KEY, body)
        self.assertIn("| 2 | fe | %s | not opened yet |" % KEY, body)
        self.assertIn("### The part of be", body)
        self.assertIn("POST /api/password-reset", body)

    def test_comments_on_the_shared_issue_say_which_repository(self):
        self.pr("be")
        self.assertTrue(self.comments("be")[-1].startswith("**[be]**"))

    def test_each_pull_request_learns_the_link_of_the_next(self):
        self.pr("be")
        self.pr("fe")
        edits = [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "edit"] and "body" in c]
        be_edit = [c for c in edits if c["argv"][2].endswith("/be/pull/7")][-1]
        self.assertIn("https://github.com/acme/fe/pull/7", be_edit["body"])
        self.assertIn("this pull request", be_edit["body"])

    def test_the_issue_waits_for_every_repository(self):
        self.pr("be")
        res = self.ok("feature", "finish", KEY)
        self.assertFalse(res["complete"])
        self.assertEqual(res["waiting"], [{"repo": "fe", "state": "running"}])
        self.assertEqual(self.transitions("be") + self.transitions("fe"), [])

    def test_the_issue_moves_once_with_every_link_in_merge_order(self):
        self.pr("fe")
        self.pr("be")
        self.fit_pass()
        res = self.ok("feature", "finish", KEY)
        self.assertTrue(res["complete"])
        self.assertEqual(res["jira_work"][0]["job"], KEY)
        self.assertIn("jira-done %s --all" % KEY, res["jira_work"][0]["when_done"])
        self.assertEqual(len(self.transitions("be")), 1)
        self.assertEqual(self.transitions("fe"), [])
        summary = self.comments("be")[-1]
        self.assertIn("ready for review in every repository", summary)
        self.assertLess(summary.index("be/pull/7"), summary.index("fe/pull/7"))
        self.assertFalse(summary.startswith("**[be]**"))
        again = self.ok("feature", "finish", KEY)
        self.assertEqual(again["jira"], {})
        self.assertEqual(len(self.transitions("be")), 1, "finish can run twice")

    def test_a_token_moves_the_issue_through_the_api(self):
        self.pr("be")
        self.pr("fe")
        self.fit_pass()
        for name in ("be", "fe"):  # what was queued before the token arrived is done
            self.ok("jira-done", KEY, "--all", repo=self.factory[name])
        calls = []
        self.give_a_token()
        with mock.patch.object(fx, "jira_transition", lambda ctx, job, which: calls.append(which) or {"moved": "x"}), \
                mock.patch.object(fx.Jira, "comment", lambda self, key, adf: {"id": "1"}):
            res = self.ok("feature", "finish", KEY)
        self.assertEqual(calls, ["review"])
        self.assertEqual(res["jira_work"], [])

    def test_a_failed_jira_call_can_be_tried_again(self):
        self.pr("be")
        self.pr("fe")
        self.fit_pass()
        self.give_a_token()
        with mock.patch.object(fx, "jira_transition", mock.Mock(side_effect=fx.FxError("down"))), \
                mock.patch.object(fx.Jira, "comment", lambda self, key, adf: {"id": "1"}):
            res = self.ok("feature", "finish", KEY)
        self.assertTrue(any("down" in n for n in res["notes"]), res["notes"])
        self.assertEqual(load_json(os.path.join(self.run_dir(), "feature.json"))["jira_finished"], [])

    def test_a_repository_not_needed_any_more_does_not_block(self):
        self.pr("be")
        self.ok("feature", "skip", KEY, "--name", "fe")
        res = self.ok("feature", "finish", KEY)
        self.assertTrue(res["complete"])
        self.assertEqual(res["fit"], "not-needed", "one branch has nothing to fit with")
        self.assertEqual(len(self.transitions("be")), 1)
        summary = self.comments("be")[-1]
        self.assertIn("No change needed in: fe", summary)
        body = slurp(os.path.join(self.factory["be"], "jobs", KEY, "pr-body.md"))
        self.assertIn("not needed", body)

    def test_skip_can_be_taken_back(self):
        self.ok("feature", "skip", KEY, "--name", "fe")
        res = self.ok("feature", "skip", KEY, "--name", "fe", "--undo")
        self.assertEqual(res["remaining"], ["be", "fe"])
        code, res = self.fx("feature", "skip", KEY, "--name", "nope")
        self.assertEqual(code, 3, res)

    def test_a_stopped_repository_holds_the_issue_back(self):
        self.pr("be")
        j = self.job("fe")
        j["stage"] = "rejected"
        write(self.job_path("fe"), json.dumps(j))
        res = self.ok("feature", "finish", KEY)
        self.assertFalse(res["complete"])
        self.assertEqual(res["waiting"], [{"repo": "fe", "state": "rejected"}])

    def test_no_folder_of_this_machine_leaves_it(self):
        self.pr("be")
        self.pr("fe")
        self.fit_pass()
        self.ok("feature", "finish", KEY)
        for name in ("be", "fe"):
            for text in [slurp(os.path.join(self.factory[name], "jobs", KEY, "pr-body.md"))] + self.comments(name):
                self.assert_no_machine_path(text)
        for call in self.gh_calls():
            if "body" in call:
                self.assert_no_machine_path(call["body"])

    def test_descriptions_of_the_other_repositories_are_scrubbed(self):
        key = "sk-ant-api03-" + "Zz9" * 10
        self.pr("be")
        write(os.path.join(self.factory["be"], "jobs", KEY, "decision.md"), "APPROVE\n\nUsed %s here.\n" % key)
        self.pr("fe")
        edits = [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "edit"] and "body" in c]
        self.assertTrue(edits)
        for call in edits:
            self.assertNotIn(key, call["body"])

    def test_a_host_that_refuses_the_edit_does_not_stop_the_run(self):
        self.pr("be")
        with mock.patch.dict(os.environ, {"GH_FAIL_EDIT": "1"}):
            res = self.pr("fe")
        self.assertTrue(any("could not update" in n for n in res["notes"]), res["notes"])
        self.assertEqual(self.job("fe")["stage"], "handed-off")

    def test_needs_a_human_shows_in_the_other_descriptions(self):
        self.pr("be")
        j = self.job("be")
        j.update(needs_human=True, handoff_reason="a decision")
        write(self.job_path("be"), json.dumps(j))
        self.pr("fe")
        body = slurp(os.path.join(self.factory["fe"], "jobs", KEY, "pr-body.md"))
        self.assertIn("needs a human", body)


class BranchesOnlyTest(MultiCase):
    """pr.create off, the default: branches are pushed and people open the pull requests."""

    def setUp(self):
        super().setUp()
        for factory in self.factory.values():
            path = os.path.join(factory, "factory.json")
            conf = load_json(path)
            conf["pr"]["create"] = False
            write(path, json.dumps(conf, indent=1))
            sh(["git", "add", "-A"], factory)
            sh(["git", "commit", "-q", "-m", "no pull requests"], factory)
        self.scope()
        self.start()
        self.new("be")
        self.new("fe")

    def test_the_description_ready_to_paste_is_kept_up_to_date(self):
        self.pr("be")
        self.pr("fe")
        body = slurp(os.path.join(self.factory["be"], "jobs", KEY, "pr-body.md"))
        self.assertIn("branch `%s`" % self.job("fe")["branch"], body)

    def test_a_pull_request_opened_by_hand_is_found(self):
        self.pr("be")
        self.pr("fe")
        self.fit_pass()
        with mock.patch.object(fx, "find_pr_url", lambda ctx, job: "https://github.com/acme/%s/pull/3" % ctx.c("project.name")):
            res = self.ok("feature", "finish", KEY)
        self.assertEqual(res["prs_updated"], ["be", "fe"])
        self.assertEqual(self.job("be")["pr_url"], "https://github.com/acme/be/pull/3")
        summary = [i["markdown"] for i in self.job("be")["jira_todo"] if i["action"] == "comment"][-1]
        self.assertIn("be/pull/3", summary)


class ChildrenTest(MultiCase):
    """A parent with one issue per repository: each issue moves by itself, the pull requests are still tied."""

    PARENT = "PROJ-100"

    def setUp(self):
        super().setUp()
        steps = [{"repo": "be", "base": "develop", "key": "PROJ-1"}, {"repo": "fe", "base": "main", "key": "PROJ-2"}]
        self.res = self.ok("feature", "start", self.PARENT, stdin=json.dumps(steps))
        for name, key in (("be", "PROJ-1"), ("fe", "PROJ-2")):
            path = os.path.join(self.tmp, key + ".md")
            write(path, "# %s: Part\n\ntext\n" % key)
            self.ok("new", key, "--feature", self.PARENT, "--issue-file", path, repo=self.factory[name])

    def test_no_part_is_needed(self):
        self.assertNotIn("scope.md", self.ok("ctx", "PROJ-2", "--agent", "builder", repo=self.factory["fe"])["prompt"])
        self.assertFalse(self.job("fe", "PROJ-2")["feature"]["shared"])

    def test_each_issue_moves_with_its_own_pull_request(self):
        res = self.pr("be", "PROJ-1")
        self.assertIn("queued", res["jira"]["transition"])
        comment = [i for i in self.job("be", "PROJ-1")["jira_todo"] if i["action"] == "comment"][-1]
        self.assertFalse(comment["markdown"].startswith("**[be]**"))

    def test_the_pull_requests_are_tied(self):
        self.pr("be", "PROJ-1")
        self.pr("fe", "PROJ-2")
        body = slurp(os.path.join(self.factory["fe"], "jobs", "PROJ-2", "pr-body.md"))
        self.assertIn("| 1 | be | PROJ-1 | https://github.com/acme/be/pull/7 |", body)
        self.assertIn("| 2 | fe | PROJ-2 | this pull request |", body)

    def test_the_finish_has_no_jira_work_of_its_own(self):
        self.pr("be", "PROJ-1")
        self.pr("fe", "PROJ-2")
        write(os.path.join(self.run_dir(self.PARENT), "fit.md"), "FIT: PASS\n")
        self.ok("feature", "fit", self.PARENT)
        write(os.path.join(self.run_dir(self.PARENT), "fit.md"), "FIT: PASS\n")
        self.ok("feature", "fit-result", self.PARENT)
        res = self.ok("feature", "finish", self.PARENT)
        self.assertTrue(res["complete"])
        self.assertEqual(res["jira"], {})


class ChildrenFromJiraTest(MultiCase):
    """Mode token on a machine without Claude's Atlassian connector: fx reads the issue's children (step 1 of /factory
    feature), and fx plan takes them as fx printed them (step 3)."""

    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.addCleanup(self.jira.stop)
        path = os.path.join(self.factory["be"], "factory.json")
        conf = load_json(path)
        conf["jira"].update(mode="token", base_url=self.jira.url)
        write(path, json.dumps(conf, indent=1))
        with fx.factory_ctx(self.factory["be"]) as c:
            env = fx.jira_env_path(c)
        write(env, "JIRA_EMAIL=%s\nJIRA_API_TOKEN=%s\n" % (EMAIL, TOKEN))
        os.chmod(env, 0o600)
        self.jira.searches['parent = "%s"' % JIRA_KEY] = [
            an_issue("PROJ-3", "FE: the reset form"), an_issue("PROJ-2", "BE: the reset endpoint"),
            an_issue("PROJ-4", "Docs: the help page")]

    def test_fx_plan_takes_them_as_fx_printed_them(self):
        children = self.ok("jira", "children", JIRA_KEY)
        self.assertEqual([c["key"] for c in children["children"]], ["PROJ-2", "PROJ-3", "PROJ-4"])
        res = self.ok("plan", stdin=json.dumps(children))
        self.assertEqual([(s["key"], s["repo"]) for s in res["steps"]], [("PROJ-2", "be"), ("PROJ-3", "fe")])
        self.assertEqual(res["unmatched"], ["PROJ-4"])

    def test_fx_plan_refuses_what_is_not_a_list_of_issues(self):
        for text in ('{"ok": true}', '"PROJ-2"', "[1, 2]"):
            code, res = self.fx("plan", stdin=text)
            self.assertEqual(code, 1, (text, res))
            self.assertIn("plan expects a JSON list of issues", res["error"])


# --------------------------------------------------------------------------
# do the branches fit each other?

class FitTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()
        self.start()
        self.new("be")
        self.new("fe")

    def both_ready(self):
        self.pr("be")
        self.pr("fe")

    def fit_copy(self, name):
        return os.path.join(self.factory[name] + "-work", "_fit-%s-%s" % (KEY, KEY))

    def test_it_waits_for_every_repository(self):
        self.pr("be")
        code, res = self.fx("feature", "fit", KEY)
        self.assertEqual(code, 3, res)
        self.assertIn("fe (running)", res["error"])

    def test_the_checker_reads_each_pushed_branch(self):
        self.both_ready()
        res = self.ok("feature", "fit", KEY)
        self.assertEqual(res["agent"], "fit-checker")
        for name in ("be", "fe"):
            copy = self.fit_copy(name)
            self.assertIn(copy, res["prompt"])
            self.assertTrue(os.path.exists(os.path.join(copy, "work-%s.txt" % name)), "the job's branch, not the base")
            self.assertIn(os.path.join(self.factory[name], "jobs", KEY, "scope.md"), res["prompt"])
            self.assertIn(os.path.join(self.factory[name], "knowledge", "stack.md"), res["prompt"])
            self.assertIn("git -C %s diff origin/main...HEAD" % copy, res["prompt"])
            diff = subprocess.run(["git", "-C", copy, "diff", "--name-only", "origin/main...HEAD"],
                                  stdout=subprocess.PIPE, text=True).stdout
            self.assertIn("work-%s.txt" % name, diff, "the command the checker is given shows the change")

    def test_the_issue_does_not_move_before_the_check(self):
        self.both_ready()
        self.assertIn("fx feature fit", self.ok("feature", "status", KEY)["next"])
        res = self.ok("feature", "finish", KEY)
        self.assertFalse(res["complete"])
        self.assertEqual(res["fit"], "missing")
        self.assertEqual(self.job("be").get("jira_todo") and
                         [i for i in self.job("be")["jira_todo"] if i.get("to") == "In Review"], [])

    def test_a_pass_opens_the_way_and_removes_the_copies(self):
        self.both_ready()
        res = self.fit_pass()
        self.assertEqual(res["result"], "PASS")
        self.assertFalse(os.path.exists(self.fit_copy("be")))
        self.assertFalse(os.path.exists(self.fit_copy("fe")))
        self.assertIn("finish", self.ok("feature", "status", KEY)["next"])
        finish = self.ok("feature", "finish", KEY)
        self.assertTrue(finish["complete"])
        self.assertEqual(finish["fit"], "PASS")
        body = slurp(os.path.join(self.factory["fe"], "jobs", KEY, "pr-body.md"))
        self.assertIn("Checked together", body)

    def test_changes_name_the_repository_to_send_back(self):
        self.both_ready()
        self.ok("feature", "fit", KEY)
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: CHANGES\n\n## Repo: fe\nThe form posts to /reset; the API is "
                                                      "/api/password-reset (be: app.py:3).\n")
        res = self.ok("feature", "fit-result", KEY)
        self.assertEqual(res["result"], "CHANGES")
        self.assertEqual(res["changes"][0]["repo"], "fe")
        self.assertEqual(res["changes"][0]["path"], self.factory["fe"])
        self.assertEqual(res["changes"][0]["job"], KEY)
        self.assertIn("/api/password-reset", res["changes"][0]["note"])
        finish = self.ok("feature", "finish", KEY)
        self.assertFalse(finish["complete"])
        self.assertEqual(finish["fit"], "CHANGES")

    def test_a_moved_branch_needs_the_check_again(self):
        self.both_ready()
        self.fit_pass()
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")
        self.ok("rework", KEY, "the form posts to the wrong path", repo=self.factory["fe"])
        j = self.job("fe")
        write(os.path.join(j["worktree"], "fix.txt"), "fixed\n")
        sh(["git", "add", "-A"], j["worktree"])
        sh(["git", "commit", "-q", "-m", "%s: fix" % KEY], j["worktree"])
        j.update(stage="pr", decision="APPROVE", gate="PASS", risk="low")
        write(self.job_path("fe"), json.dumps(j, indent=1))
        self.ok("pr", KEY, "--target", "main", repo=self.factory["fe"])
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")
        self.assertFalse(self.ok("feature", "finish", KEY)["complete"])
        self.assertEqual(self.fit_pass()["rounds"], 2)
        self.assertTrue(self.ok("feature", "finish", KEY)["complete"])

    def test_a_human_can_accept_going_without_it_and_it_is_said_out_loud(self):
        self.both_ready()
        res = self.ok("feature", "finish", KEY, "--accept-fit", "the app team checks it by hand")
        self.assertTrue(res["complete"])
        self.assertEqual(res["fit"], "accepted")
        summary = [i["markdown"] for i in self.job("be")["jira_todo"] if i["action"] == "comment"][-1]
        self.assertIn("not checked together", summary)
        self.assertIn("the app team checks it by hand", summary)
        body = slurp(os.path.join(self.factory["fe"], "jobs", KEY, "pr-body.md"))
        self.assertIn("**not checked**", body)
        self.assertIn("the app team checks it by hand", body)

    def assert_bad_fit(self, text, say):
        self.ok("feature", "fit", KEY)
        write(os.path.join(self.run_dir(), "fit.md"), text)
        code, res = self.fx("feature", "fit-result", KEY)
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "fit-checker")
        self.assertIn(say, res["error"])

    def test_a_bad_result_is_sent_back(self):
        self.both_ready()
        self.assert_bad_fit("Looks fine\n", "not a valid")
        self.assert_bad_fit("FIT: CHANGES\n\nSomething is off.\n", "## Repo")
        self.assert_bad_fit("FIT: CHANGES\n\n## Repo: app\nx\n", "not app")
        self.assert_bad_fit("FIT: CHANGES\n\n## Repo: fe\n\n", "## Repo")

    def test_no_result_file_is_a_retry(self):
        self.both_ready()
        self.ok("feature", "fit", KEY)
        code, res = self.fx("feature", "fit-result", KEY)
        self.assertEqual(code, 6, res)

    def test_an_old_result_is_not_read_as_the_new_one(self):
        self.both_ready()
        self.fit_pass()
        self.ok("feature", "fit", KEY)
        code, res = self.fx("feature", "fit-result", KEY)
        self.assertEqual(code, 6, res)


class StartDoesNotWaitOrStopTest(MultiCase):
    def old_baseline(self, name):
        conf = load_json(os.path.join(self.factory[name], "factory.json"))
        conf["app"] = {"up": "true"}
        conf["security"]["scanners"] = {"zap": False}
        write(os.path.join(self.factory[name], "factory.json"), json.dumps(conf, indent=1))
        sh(["git", "add", "-A"], self.factory[name])
        sh(["git", "commit", "-q", "-m", "app"], self.factory[name])
        write(os.path.join(self.factory[name], "baseline.json"), json.dumps({"security": {"headers": []}}))

    def test_a_baseline_from_before_does_not_refuse_the_run(self):
        self.old_baseline("be")
        self.scope()
        self.assertTrue(self.start()["ok"])

    def test_fx_doctor_itself_does_say_it(self):
        self.old_baseline("be")
        res = self.fx("doctor", repo=self.factory["be"])[1]
        self.assertFalse(next(c for c in res["checks"] if c["check"] == "headers baseline usable")["ok"])

    def test_it_does_not_try_the_scanners_even_for_this_factory(self):
        self.scope()
        with mock.patch.object(fx, "scanner_probes") as tried:
            self.start()
        self.assertFalse(tried.called)
        with mock.patch.object(fx, "scanner_probes", return_value=[]) as tried:
            self.fx("doctor", repo=self.factory["be"])
        self.assertTrue(tried.called)


class SmallThingsTest(MultiCase):
    def test_repositories_are_counted_not_steps(self):
        steps = [{"repo": "be", "base": "main", "key": "PROJ-10"}, {"repo": "be", "base": "main", "key": "PROJ-11"},
                 {"repo": "fe", "base": "main", "key": "PROJ-12"}]
        self.ok("feature", "start", "PROJ-100", stdin=json.dumps(steps))
        path = os.path.join(self.tmp, "PROJ-10.md")
        write(path, "# PROJ-10: Part\n\ntext\n")
        self.ok("new", "PROJ-10", "--feature", "PROJ-100", "--issue-file", path, repo=self.factory["be"])
        self.pr("be", "PROJ-10")
        body = slurp(os.path.join(self.factory["be"], "jobs", "PROJ-10", "pr-body.md"))
        self.assertIn("needs changes in 2 repositories", body)

    def test_finish_names_what_needs_a_human(self):
        self.scope()
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        self.new("fe")
        self.to_pr("fe")
        j = self.job("fe")
        j.update(needs_human=True, handoff_reason="sensitive paths")
        write(self.job_path("fe"), json.dumps(j))
        self.ok("pr", KEY, "--target", "main", repo=self.factory["fe"])
        self.pr("be")
        self.fit_pass()
        res = self.ok("feature", "finish", KEY)
        self.assertEqual(res["needs_human"], ["fe"])
        self.assertIn(KEY, res["jira"])  # the issue still moves, as with one repository

    def test_a_route_that_only_looks_like_a_machine_path_stays(self):
        run_ = {"steps": []}
        for route in ("/home/feed/latest", "/api/home/feed/latest", "/Users/1/orders", "/v1/Users/a/b", "x/home/a/b",
                      "/tmpfiles/a", "/tmp-x/a", "/api/tmp/a"):
            self.assertEqual(fx.feature_no_machine_paths(run_, "GET %s here" % route, "be"), "GET %s here" % route)

    def clean(self, text, home=None, steps=()):
        env = {"HOME": home} if home else {}
        with mock.patch.dict(os.environ, env):
            return fx.feature_no_machine_paths({"steps": list(steps)}, text, "be")

    def test_what_fx_knows_to_be_this_machines_goes(self):
        for home in ("/Users/linh", "/home/linh", "/mnt/c/Users/linh", "/root"):
            with self.subTest(home=home):
                self.assertEqual(self.clean("see %s/code/x.py now" % home, home), "see be now")
                self.assertEqual(self.clean("see `%s/code/x.py`." % home, home), "see `be`.")
                self.assertEqual(self.clean("see %s now" % home, home), "see be now")
        self.assertEqual(self.clean("see ~/code/x.py now"), "see be now")
        self.assertEqual(self.clean("see file:///home/linh/code/x.py now", "/home/linh"), "see be now")
        self.assertEqual(self.clean("in /tmp/fx-abc/x and /var/tmp/y"), "in be and be")
        self.assertEqual(self.clean("in %s/fx-abc/x" % tempfile.gettempdir()), "in be")

    def test_a_home_with_more_after_its_name_is_someone_else(self):
        self.assertEqual(self.clean("see /home/linhdan/x", "/home/linh"), "see /home/linhdan/x")

    def test_a_home_that_is_the_root_of_everything_is_no_folder(self):
        self.assertEqual(self.clean("GET /api/a", "/"), "GET /api/a")

    def test_a_repositorys_folders_become_its_name(self):
        step = {"path": "/Volumes/Data/code/fe-factory", "repo": "fe"}
        self.assertEqual(self.clean("in /Volumes/Data/code/fe-factory-work/T-1/a.js", steps=[step]), "in fe/T-1/a.js")
        self.assertEqual(self.clean("in /Volumes/Data/code/fe-factory/knowledge", steps=[step]), "in fe/knowledge")
        self.assertEqual(self.clean("in /Volumes/Data/code/fe-factory2/x", steps=[step]), "in /Volumes/Data/code/fe-factory2/x")

    def test_so_do_those_of_the_workspace(self):
        self.assertEqual(self.clean("as in %s/a.py" % self.factory["be"]), "as in be/a.py")
        self.assertEqual(self.clean("as in %s-work/T-1/a.py" % self.factory["fe"]), "as in fe/T-1/a.py")

    def test_machine_paths_stay_out_of_the_pull_request(self):
        text = SCOPE.replace("Add POST /api/password-reset.", "Add POST /api/password-reset in %s/server.js, as in "
                             "%s/code/other/x.py; see /home/index." % (self.factory["be"], os.environ["HOME"]))
        self.assertNotEqual(text, SCOPE)
        self.scope(text)
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        self.pr("be")
        body = slurp(os.path.join(self.factory["be"], "jobs", KEY, "pr-body.md"))
        self.assertNotIn(self.factory["be"], body)
        self.assertNotIn(os.environ["HOME"] + "/code", body)
        self.assertIn("/api/password-reset", body)
        self.assertIn("/home/index", body)
        self.assertIn("in be/server.js", body)


class SameOrderTest(MultiCase):
    repos = (("be", 50), ("fe", 50))

    def test_the_later_repository_still_gets_the_notes(self):
        self.scope()
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        write(os.path.join(self.factory["be"], "jobs", KEY, "api-notes.md"), "POST /api/password-reset\n")
        self.pr("be")
        self.new("fe")
        ups = self.job("fe")["upstream"]
        self.assertEqual(len(ups), 1)
        self.assertIn("POST /api/password-reset", slurp(ups[0]))

    def test_the_earlier_one_does_not_get_its_own(self):
        self.scope()
        self.start({"be": "main", "fe": "main"})
        self.new("fe")
        write(os.path.join(self.factory["fe"], "jobs", KEY, "api-notes.md"), "notes\n")
        self.new("be")
        self.assertEqual(self.job("be")["upstream"], [])


class NewJobStopsCleanlyTest(MultiCase):
    def job_dir(self, job=KEY):
        return os.path.join(self.factory["be"], "jobs", job)

    def clone(self, job=KEY):
        return os.path.join(self.factory["be"] + "-work", job)

    def assert_no_trace(self):
        self.assertFalse(os.path.exists(self.job_dir()))
        self.assertFalse(os.path.exists(self.clone()))

    def test_a_part_that_is_gone_stops_it_before_anything_exists(self):
        self.scope()
        self.start({"be": "main", "fe": "main"})
        os.remove(fx.feature_scope_path(KEY, "be"))
        code, res = self.fx("new", KEY, "--feature", KEY, "--issue-file", self.issue_file(), repo=self.factory["be"])
        self.assertEqual(code, 3, res)
        self.assertIn("no part for be", res["error"])
        self.assert_no_trace()

    def test_trouble_after_the_clone_leaves_nothing_behind(self):
        self.scope()
        self.start({"be": "main", "fe": "main"})
        with mock.patch.object(fx, "feature_job_block", side_effect=fx.FxError("no")):
            code, res = self.fx("new", KEY, "--feature", KEY, "--issue-file", self.issue_file(), repo=self.factory["be"])
        self.assertNotEqual(code, 0, res)
        self.assert_no_trace()

    def half_a_job(self):
        write(os.path.join(self.job_dir("PROJ-77"), "issue.md"), "stopped halfway\n")
        path = os.path.join(self.tmp, "i.md")
        write(path, "# PROJ-77: x\n\ntext\n")
        return path

    def test_again_works_on_a_folder_with_no_job(self):
        path = self.half_a_job()
        res = self.ok("new", "PROJ-77", "--again", "--issue-file", path, repo=self.factory["be"])
        self.assertEqual(res["job"], "PROJ-77-r2")

    def test_without_again_it_says_what_to_do(self):
        path = self.half_a_job()
        code, res = self.fx("new", "PROJ-77", "--issue-file", path, repo=self.factory["be"])
        self.assertEqual(code, 2, res)
        self.assertIn("--again", res["error"])


class StartingAgainTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()

    def test_a_step_with_a_job_cannot_be_dropped(self):
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        self.new("fe")
        code, res = self.fx("feature", "start", KEY, stdin=json.dumps([{"repo": "be", "base": "main"}]))
        self.assertEqual(code, 3, res)
        self.assertIn("fx feature skip", res["error"])
        self.assertEqual(res["has_job"], ["fe %s (job %s)" % (KEY, KEY)])
        self.assertEqual([s["repo"] for s in self.ok("feature", "status", KEY)["steps"]], ["be", "fe"])

    def test_a_step_with_no_job_yet_can(self):
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        res = self.start({"be": "main"})
        self.assertEqual([s["repo"] for s in res["steps"]], ["be"])

    def test_a_job_that_had_the_whole_issue_gets_its_part_and_says_so(self):
        self.start({"be": "main"})
        self.new("be")
        self.assertFalse(self.job("be")["feature"]["shared"])
        res = self.start({"be": "main", "fe": "main"})
        self.assertTrue(any("started when it had the whole issue" in n for n in res["notes"]), res["notes"])
        self.assertTrue(self.job("be")["feature"]["shared"])
        self.assertEqual(self.job("be")["feature"]["others"], ["fe"])
        self.assertTrue(os.path.exists(os.path.join(self.factory["be"], "jobs", KEY, "scope.md")))

    def test_its_pull_request_does_not_move_the_issue(self):
        self.start({"be": "main"})
        self.new("be")
        self.start({"be": "main", "fe": "main"})
        self.pr("be")
        moved = [i for i in self.job("be").get("jira_todo", []) if i["action"] == "transition" and i["to"] == "In Review"]
        self.assertEqual(moved, [])

    def test_the_pull_request_goes_by_the_run_whatever_the_job_noted(self):
        self.start({"be": "main", "fe": "main"})
        self.new("be")
        j = self.job("be")
        j["feature"]["shared"] = False  # an older job, or a note that fell behind
        write(self.job_path("be"), json.dumps(j, indent=1))
        self.pr("be")
        moved = [i for i in self.job("be").get("jira_todo", []) if i["action"] == "transition" and i["to"] == "In Review"]
        self.assertEqual(moved, [])


class FitOfTwoIssuesInOneRepoTest(MultiCase):
    PARENT = "PROJ-100"

    def setUp(self):
        super().setUp()
        steps = [{"repo": "be", "base": "main", "key": "PROJ-10"}, {"repo": "be", "base": "main", "key": "PROJ-11"},
                 {"repo": "fe", "base": "main", "key": "PROJ-12"}]
        self.ok("feature", "start", self.PARENT, stdin=json.dumps(steps))
        for repo, key in (("be", "PROJ-10"), ("be", "PROJ-11"), ("fe", "PROJ-12")):
            path = os.path.join(self.tmp, key + ".md")
            write(path, "# %s: Part\n\ntext\n" % key)
            self.ok("new", key, "--feature", self.PARENT, "--issue-file", path, repo=self.factory[repo])
            self.pr(repo, key)

    def copy(self, key):
        return os.path.join(self.factory["be"] + "-work", "_fit-%s-%s" % (self.PARENT, key))

    def test_each_job_has_its_own_copy_on_its_own_branch(self):
        res = self.ok("feature", "fit", self.PARENT)
        for key in ("PROJ-10", "PROJ-11"):
            self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.copy(key)).strip(),
                             self.job("be", key)["branch"])
            self.assertIn("- be (%s), issue %s: copy %s," % (key, key, self.copy(key)), res["prompt"])

    def test_a_result_goes_to_the_job_it_names(self):
        self.ok("feature", "fit", self.PARENT)
        write(os.path.join(self.run_dir(self.PARENT), "fit.md"), "FIT: CHANGES\n\n## Repo: be (PROJ-11)\nThe route is wrong.\n")
        res = self.ok("feature", "fit-result", self.PARENT)
        self.assertEqual([(c["repo"], c["issue"], c["job"]) for c in res["changes"]], [("be", "PROJ-11", "PROJ-11")])
        self.assertFalse(os.path.exists(self.copy("PROJ-10")))
        self.assertFalse(os.path.exists(self.copy("PROJ-11")))

    def test_a_section_that_does_not_say_which_issue_goes_back_to_the_checker(self):
        self.ok("feature", "fit", self.PARENT)
        write(os.path.join(self.run_dir(self.PARENT), "fit.md"), "FIT: CHANGES\n\n## Repo: be\nThe route is wrong.\n")
        code, res = self.fx("feature", "fit-result", self.PARENT)
        self.assertEqual(code, 6, res)
        self.assertIn("be (PROJ-10)", res["error"])
        self.assertEqual(res["retry_agent"], "fit-checker")


class BranchMovedAfterTheCheckTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()
        self.start()
        self.new("be")
        self.new("fe")
        self.pr("be")
        self.pr("fe")

    def push_from_elsewhere(self):
        other = os.path.join(self.tmp, "someone-else")
        sh(["git", "clone", "-q", self.origin["fe"], other], self.tmp)
        branch = self.job("fe")["branch"]
        sh(["git", "checkout", "-q", branch], other)
        write(os.path.join(other, "late.txt"), "late\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "-c", "user.name=h", "-c", "user.email=h@example.com", "commit", "-q", "-m", "late"], other)
        sh(["git", "push", "-q", "origin", branch], other)

    def assert_needs_a_new_check(self):
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")
        res = self.ok("feature", "finish", KEY)
        self.assertFalse(res["complete"])
        self.assertEqual(res["fit"], "stale")
        self.assertIn("skipped", res["jira"])

    def test_someone_pushes_after_the_check(self):
        self.fit_pass()
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")
        self.push_from_elsewhere()
        self.assert_needs_a_new_check()

    def test_someone_pushes_while_the_checker_is_reading(self):
        self.ok("feature", "fit", KEY)
        self.push_from_elsewhere()
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: PASS\n\n## Checked\nx\n")
        self.ok("feature", "fit-result", KEY)
        self.assert_needs_a_new_check()

    def test_rework_while_the_checker_is_reading(self):
        self.ok("feature", "fit", KEY)
        self.ok("rework", KEY, "note", repo=self.factory["fe"])
        j = self.job("fe")
        write(os.path.join(j["worktree"], "fix.txt"), "fixed\n")
        sh(["git", "add", "-A"], j["worktree"])
        sh(["git", "commit", "-q", "-m", "%s: fix" % KEY], j["worktree"])
        j.update(stage="pr", decision="APPROVE", gate="PASS", risk="low")
        write(self.job_path("fe"), json.dumps(j, indent=1))
        self.ok("pr", KEY, "--target", "main", repo=self.factory["fe"])
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: PASS\n\n## Checked\nx\n")
        self.ok("feature", "fit-result", KEY)
        self.assert_needs_a_new_check()

    def merge_and_delete(self, name):
        origin, branch = self.origin[name], self.job(name)["branch"]
        sh(["git", "update-ref", "refs/heads/main", "refs/heads/%s" % branch], origin)
        sh(["git", "branch", "-q", "-D", branch], origin)

    def test_a_merged_and_deleted_branch_has_not_moved(self):
        self.fit_pass()
        self.merge_and_delete("fe")
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")
        self.assertTrue(self.ok("feature", "finish", KEY)["complete"])

    def test_nor_when_someone_else_pushed_last_before_the_check(self):
        self.push_from_elsewhere()
        self.fit_pass()
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")
        self.merge_and_delete("fe")
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")
        self.assertTrue(self.ok("feature", "finish", KEY)["complete"])

    def delete_on_the_remote(self, name="fe"):
        branch = self.job(name)["branch"]
        tip = sh(["git", "rev-parse", "refs/heads/%s" % branch], self.origin[name]).strip()
        sh(["git", "branch", "-q", "-D", branch], self.origin[name])
        return tip

    def test_a_host_that_says_merged_is_enough(self):
        """A squash merge leaves nothing in the target's history to find."""
        self.push_from_elsewhere()
        self.fit_pass()
        tip = self.delete_on_the_remote()
        with mock.patch.object(fx, "pr_feedback", return_value=({"state": "merged", "head": tip}, None)):
            self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")

    def test_a_host_that_says_it_was_merged_at_another_head(self):
        """What was merged is not what was checked."""
        self.fit_pass()
        self.push_from_elsewhere()
        late = self.delete_on_the_remote()
        with mock.patch.object(fx, "pr_feedback", return_value=({"state": "merged", "head": late}, None)):
            self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")

    def test_a_host_that_says_closed(self):
        self.fit_pass()
        self.delete_on_the_remote()
        with mock.patch.object(fx, "pr_feedback", return_value=({"state": "closed"}, None)):
            self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")
            code, res = self.fx("feature", "fit", KEY)
        self.assertEqual(code, 3, res)
        self.assertIn("closed without a merge", res["error"])
        self.assertIn("--accept-fit", res["error"])
        self.assertNotIn("could not clone", res["error"])

    def test_a_squash_merge_with_no_word_from_the_host_needs_a_new_check_that_can_run(self):
        self.fit_pass()
        write_tree = sh(["git", "rev-parse", "refs/heads/%s^{tree}" % self.job("fe")["branch"]], self.origin["fe"]).strip()
        squashed = sh(["git", "-c", "user.name=h", "-c", "user.email=h@example.com", "commit-tree", write_tree, "-p",
                       "refs/heads/main", "-m", "squash"], self.origin["fe"]).strip()
        sh(["git", "update-ref", "refs/heads/main", squashed], self.origin["fe"])
        self.delete_on_the_remote()
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")
        res = self.ok("feature", "fit", KEY)
        self.assertIn("nobody could say whether it was merged", json.dumps(res))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fe-factory-work", "_fit-%s-%s" % (KEY, self.job("fe")["id"]),
                                                    "work-fe.txt")))
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: PASS\n\n## Checked\nx\n")
        self.ok("feature", "fit-result", KEY)
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")

    def test_a_branch_deleted_without_a_merge_needs_a_new_check(self):
        self.fit_pass()
        sh(["git", "branch", "-q", "-D", self.job("fe")["branch"]], self.origin["fe"])
        self.assert_needs_a_new_check()

    def test_a_new_check_after_a_merge_reads_the_branch_it_went_into(self):
        self.fit_pass()
        self.merge_and_delete("be")
        self.push_from_elsewhere()  # the other one moved: this needs a new check
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")
        res = self.ok("feature", "fit", KEY)
        self.assertIn("already merged", json.dumps(res))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "be-factory-work", "_fit-%s-%s" % (KEY, self.job("be")["id"]), "work-be.txt")))
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: PASS\n\n## Checked\nx\n")
        self.ok("feature", "fit-result", KEY)
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "PASS")

    def test_a_remote_nobody_can_read_counts_as_moved(self):
        self.fit_pass()
        os.rename(self.origin["fe"], self.origin["fe"] + ".gone")
        self.assertEqual(self.ok("feature", "status", KEY)["fit"], "stale")

    def test_a_result_with_no_check_running_is_refused(self):
        write(os.path.join(self.run_dir(), "fit.md"), "FIT: PASS\n\n## Checked\nx\n")
        code, res = self.fx("feature", "fit-result", KEY)
        self.assertEqual(code, 3, res)
        self.assertIn("no check is running", res["error"])


class GitlabTest(MultiCase):
    def test_a_merge_request_is_updated_without_its_clone(self):
        with mock.patch.dict(os.environ, {}):
            path = os.path.join(self.factory["be"], "factory.json")
            conf = load_json(path)
            conf["project"]["url"] = "git@gitlab.example.com:group/sub/be.git"
            conf["pr"]["provider"] = "gitlab"
            write(path, json.dumps(conf, indent=1))
            os.chdir(self.factory["be"])
            ctx = fx.Ctx()
            os.chdir(self.cwd)
            body = os.path.join(self.tmp, "body.md")
            write(body, "text\n")
            problem = fx.pr_set_body(ctx, {}, "https://gitlab.example.com/group/sub/be/-/merge_requests/12", body)
        self.assertIsNone(problem)
        call = self.gh_calls()[-1]
        self.assertEqual(call["argv"][:3], ["mr", "update", "12"])
        self.assertEqual(call["argv"][call["argv"].index("--repo") + 1], "https://gitlab.example.com/group/sub/be")


if __name__ == "__main__":
    unittest.main()
