"""Tests for the agent prompts, the pull requests and the learning of a job over several repositories (standard library
only). Run from the repository root:
    python3 -I -m unittest discover -s tests

Every prompt lists the job's repositories and their knowledge; fx pr makes every changed repository ready before it
pushes any, opens one pull request per repository and links them to each other; learning reads each repository's pull
request and the edits humans pushed to its branch. Every origin is a real bare repository; gh and the scanners are stubs.
"""

import json
import os
import stat
import subprocess
import sys
import unittest
from unittest import mock

from test_project_repos import KEY, ProjectCase
from test_security import fx, load_json, sh, slurp, write

JIRA = "https://example.atlassian.net"
GH_STUB = r'''#!%(python)s -I
import json, os, subprocess, sys
args = sys.argv[1:]
rec = {"argv": args, "cwd": os.getcwd()}
if "--body-file" in args:
    rec["body"] = open(args[args.index("--body-file") + 1]).read()
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
state = json.load(open(os.environ["GH_STATE"])) if os.path.exists(os.environ["GH_STATE"]) else {}
if args[:2] == ["pr", "create"]:
    origin = subprocess.run(["git", "remote", "get-url", "origin"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print("https://github.com/acme/%%s/pull/7" %% os.path.basename(origin).replace(".git", ""))
elif args[:2] == ["pr", "view"]:
    pr = state.get(args[2])
    if not pr or args[args.index("--json") + 1] == "url":
        sys.exit(1)
    print(json.dumps({"state": pr.get("state", "OPEN"), "headRefOid": pr.get("head"), "number": 7, "reviews": [],
                      "comments": pr.get("comments", [])}))
elif args[:1] == ["api"]:
    print("[]")
'''
# gitleaks that finds a secret in the clone of fe only
LEAKS_IN_FE = r'''#!%(python)s -I
import json, os, sys
args = sys.argv[1:]
if "--report-path" in args:
    hits = []
    if os.sep + "fe" + os.sep in (os.environ.get("GIT_DIR") or ""):
        hits = [{"File": "config.js", "RuleID": "generic-api-key", "StartLine": 3, "Commit": "abc"}]
    with open(args[args.index("--report-path") + 1], "w") as f:
        json.dump(hits, f)
'''
LOGIC_FILE = ("# Reset\n\n## 1. Goal\nReset a password.\n\n## 2. Rules\n- One link per request.\n\n## 3. Flow\n"
              "```mermaid\nflowchart LR\n  a-->b\n```\n\n## 4. Data\nNone.\n\n## 5. Tests\nNone.\n")
SPEC = """# Spec

## Goal
Reset a password.

## Acceptance criteria
1. be: POST /api/reset answers 202.
2. fe: the form calls it.

## Repo: be
Add POST /api/reset.

## Repo: fe
Add the reset form.

## Shared contract
POST /api/reset takes {"email": string} and answers 202.
"""


class PromptCase(ProjectCase):
    def setUp(self):
        super().setUp()
        k = os.path.join(self.factory, "knowledge")
        for rel in ("README.md", "overview.md", "security.md", "be/README.md", "be/api.md", "be/architecture.md",
                    "fe/README.md", "fe/ui.md", "fe/architecture.md"):
            write(os.path.join(k, rel), "# %s\n" % rel)
        self.commit_factory()

    def job_for(self, repos="be,fe"):
        job = self.new("--repos", repos)["job"]
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j.update(stage="build", risk="high")
        fx.route(ctx, j)
        ctx.save(j)
        return job

    def prompt(self, job, agent, *more):
        return self.ok("ctx", job, "--agent", agent, *more)["prompt"]

    def k(self, rel):
        return os.path.join(self.factory, "knowledge", rel)

    def knowledge(self, prompt):
        line = [l for l in prompt.splitlines() if l.startswith("Project knowledge")][0]
        return line.split(": ", 1)[1].split(", ")


class ReposBlockTest(PromptCase):
    def test_every_prompt_lists_the_job_s_repositories(self):
        job = self.job_for()
        raw = self.raw(job)
        for agent in ("builder", "code", "security", "approver", "judge", "planner", "fit"):
            p = self.prompt(job, agent)
            for n in ("be", "fe"):
                e = raw["repos"][n]
                row = [l for l in p.splitlines() if l.startswith("- %s: change" % n)]
                self.assertEqual(len(row), 1, (agent, p))
                for part in ("worktree %s" % e["worktree"], "branch %s" % e["branch"], "base %s" % e["base_ref"],
                             "checks script %s" % os.path.join(self.factory, "checks", "%s.sh" % n)):
                    self.assertIn(part, row[0], agent)
            self.assertNotIn("Worktree: ", p)
            self.assertIn("Factory repository (never edit it): %s" % self.factory, p)
        self.assertIn("change only the ones whose role is change", self.prompt(job, "builder"))
        self.assertIn("read them, never change them", self.prompt(job, "code"))

    def test_a_run_only_repository_is_marked(self):
        cfg = self.config()
        cfg["repos"][0]["app"] = {"up": "serve-be {port}"}
        cfg["repos"][1]["app"] = {"up": "serve-fe {port}", "needs": ["be"]}
        self.write_config(cfg)
        self.commit_factory()
        job = self.job_for("fe")
        p = self.prompt(job, "builder")
        row = [l for l in p.splitlines() if l.startswith("- be: ")][0]
        self.assertIn("be: run (a checkout of its base only to run its app; never change it)", row)
        self.assertNotIn("checks script", row)
        port = self.raw(job)["port"]
        self.assertIn("app http://127.0.0.1:%d" % port, row)
        self.assertIn("app http://127.0.0.1:%d" % (port + 1), [l for l in p.splitlines() if l.startswith("- fe: ")][0])
        self.assertIn("Running apps: `%s app up %s` starts the job's apps (be, fe)" % (fx.FX_CMD, job), p)
        self.assertIn(os.path.join(self.factory, "jobs", job, "app-be.log"), p)

    def test_a_job_of_one_repository_reads_as_before(self):
        job = self.job_for("be")
        p = self.prompt(job, "builder")
        self.assertIn("Worktree: %s" % self.work(job, "be"), p)
        self.assertIn("Checks script: %s" % os.path.join(self.factory, "checks", "be.sh"), p)
        self.assertNotIn("Repositories of this job", p)
        self.assertNotIn("Shared contract", p)

    def test_dev_ports_are_the_agents_not_the_apps(self):
        cfg = self.config()
        cfg["repos"][1]["dev_server"] = "npm run dev -- --port {port}"
        self.write_config(cfg)
        self.commit_factory()
        job = self.job_for()
        raw = self.raw(job)
        ports = {}
        for agent in ("builder", "ux", "ui"):
            p = self.prompt(job, agent)
            ports[agent] = int([l for l in p.splitlines() if l.startswith("Port: ")][0].split()[1])
            self.assertIn("dev server `npm run dev -- --port {port}`", p)
        self.assertEqual(ports, {"builder": raw["port"] + 2, "ux": raw["port"] + 3, "ui": raw["port"] + 4})


class KnowledgePerRepoTest(PromptCase):
    def test_shared_then_each_repository_of_the_job(self):
        job = self.job_for()
        got = self.knowledge(self.prompt(job, "builder"))
        self.assertEqual(got, [self.k("README.md"), self.k("overview.md"), self.k("security.md"),
                               self.k("be/README.md"), self.k("be/api.md"), self.k("be/architecture.md"),
                               self.k("fe/README.md"), self.k("fe/architecture.md"), self.k("fe/ui.md")])
        code = self.knowledge(self.prompt(job, "code"))
        self.assertIn(self.k("be/api.md"), code)
        self.assertNotIn(self.k("fe/ui.md"), code)  # ui.md is not the code reviewer's

    def test_a_repository_the_job_does_not_have_is_left_out(self):
        job = self.job_for("be")
        got = self.knowledge(self.prompt(job, "builder"))
        self.assertIn(self.k("be/api.md"), got)
        self.assertFalse([f for f in got if os.sep + "fe" + os.sep in f], got)

    def test_intake_and_the_spec_read_every_repository(self):
        job = self.job_for("be")
        spec = self.knowledge(self.prompt(job, "spec"))
        self.assertIn(self.k("fe/ui.md"), spec)
        self.assertIn(self.k("fe/README.md"), spec)
        job2 = self.new()["job"]  # the repositories wait for intake
        intake = self.knowledge(self.prompt(job2, "intake"))
        self.assertIn(self.k("fe/README.md"), intake)
        self.assertIn(self.k("be/README.md"), intake)
        self.assertNotIn(self.k("fe/ui.md"), intake)

    def test_the_fit_checker_reads_architecture_and_api_of_each(self):
        job = self.job_for()
        got = self.knowledge(self.prompt(job, "fit"))
        for rel in ("be/api.md", "be/architecture.md", "fe/architecture.md"):
            self.assertIn(self.k(rel), got)


class OldFlatKnowledgeTest(PromptCase):
    names = ("be",)

    def setUp(self):
        super().setUp()
        for rel in ("api.md", "stack.md", "ui.md"):
            write(os.path.join(self.factory, "knowledge", rel), "# %s\n" % rel)
        self.commit_factory()

    def test_flat_files_count_as_the_one_repository_s(self):
        job = self.job_for("be")
        got = self.knowledge(self.prompt(job, "builder"))
        for rel in ("api.md", "stack.md", "ui.md"):
            self.assertIn(self.k(rel), got)
        self.assertNotIn("Repositories of this job", self.prompt(job, "builder"))


class SpecAndFitPromptTest(PromptCase):
    def test_the_spec_writer_is_told_of_the_parts_and_the_contract(self):
        p = self.prompt(self.job_for(), "spec")
        self.assertIn("give spec.md a `## Repo: <name>` part for each", p)
        self.assertIn("`## Shared contract`", p)
        self.assertIn("(be, fe)", p)
        self.assertNotIn("## Repo: <name>", self.prompt(self.job_for("be"), "spec"))

    def test_the_builder_keeps_to_the_contract_and_no_upstream_notes(self):
        job = self.job_for()
        os.makedirs(os.path.join(self.factory, "jobs", job, "upstream"))
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["upstream"] = [os.path.join(self.factory, "jobs", job, "upstream", "1-api-notes.md")]
        ctx.save(j)
        p = self.prompt(job, "builder")
        self.assertIn("Shared contract between the repositories: `## Shared contract` in %s" % os.path.join(
            self.factory, "jobs", job, "spec.md"), p)
        self.assertNotIn("Upstream API notes", p)
        self.assertNotIn("Other jobs do the rest", p)

    def test_the_fit_checker_gets_the_issue_spec_clones_and_its_format(self):
        job = self.job_for()
        raw = self.raw(job)
        res = self.ok("ctx", job, "--agent", "fit")
        p = res["prompt"]
        self.assertIn("Jira issue" if raw.get("jira_base") else "Task: %s (issue.md in the job folder" % job, p)
        self.assertIn("Spec: %s, with a `## Repo: <name>` part" % os.path.join(self.factory, "jobs", job, "spec.md"), p)
        for n in ("be", "fe"):
            self.assertIn("worktree %s" % raw["repos"][n]["worktree"], p)
            self.assertIn("base %s" % raw["repos"][n]["base_ref"], p)
        self.assertIn("read them, never change them", p)
        self.assertIn("Result file: %s" % os.path.join(self.factory, "jobs", job, "round-0", "review-fit.md"), p)
        self.assertIn("first line `VERDICT: PASS` or `VERDICT: CHANGES`; then `## Checked`", p)
        self.assertIn("`## Repo: <name>`", p)
        self.assertEqual(res["model"], raw["models"]["fit"])

    def test_a_candidate_sees_its_own_clone_of_every_repository(self):
        cfg = self.config()
        cfg["repos"][0]["app"] = {"up": "serve-be {port}"}
        self.write_config(cfg)
        self.commit_factory()
        job = self.job_for()
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["best_of"] = 2
        ctx.save(j)
        self.ok("candidates", job)
        raw = self.raw(job)
        p = self.prompt(job, "builder", "--candidate", "2")
        for n in ("be", "fe"):
            c = [c for c in raw["repos"][n]["candidates"] if c["k"] == 2][0]
            row = [l for l in p.splitlines() if l.startswith("- %s: change" % n)][0]
            self.assertIn("worktree %s" % c["worktree"], row)
            self.assertIn("branch %s" % c["branch"], row)
        be = [c for c in raw["repos"]["be"]["candidates"] if c["k"] == 2][0]
        self.assertIn("app http://127.0.0.1:%d" % be["port"], p)
        self.assertIn("Report file: %s" % os.path.join(self.factory, "jobs", job, "build-c2.md"), p)

    def test_the_planner_names_a_repository_for_each_package(self):
        self.assertIn('give every package of the json block its "repo"', self.prompt(self.job_for(), "planner"))


# --------------------------------------------------------------------------
# fx pr: every repository ready before any push, one pull request each, linked

class PrCase(ProjectCase):
    top = {"pr": {"create": True, "provider": "github", "labels": []},
           "jira": {"base_url": JIRA, "transitions": {"start": "In Progress", "review": "In Review"}}}

    def setUp(self):
        super().setUp()
        self.stub("gh", GH_STUB)
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.gh_state = os.path.join(self.tmp, "gh-state.json")
        os.environ.update(GH_LOG=self.gh_log, GH_STATE=self.gh_state)
        write(self.issue, "# %s: Reset password\n\n- Link: %s/browse/%s\n\nBoth sides change.\n" % (KEY, JIRA, KEY))

    def stub(self, name, text):
        path = os.path.join(self.bin, name)
        if os.path.lexists(path):
            os.remove(path)
        write(path, text % {"python": sys.executable})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def jira_job(self, repos="be,fe"):
        res = self.ok("new", KEY, "--issue-file", self.issue, "--repos", repos)
        return res["job"]

    def to_pr(self, job, names=("be", "fe")):
        for n in names:
            self.commit_in(self.work(job, n), "%s.txt" % n, "work\n", "%s work" % n)
        write(os.path.join(self.factory, "jobs", job, "spec.md"), SPEC)
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j.update(stage="pr", decision="APPROVE", gate="PASS", risk="low")
        ctx.save(j)
        return len(j.get("jira_todo") or [])

    def gh_calls(self, verb=None):
        if not os.path.exists(self.gh_log):
            return []
        calls = [json.loads(l) for l in slurp(self.gh_log).splitlines()]
        return [c for c in calls if verb is None or c["argv"][:2] == ["pr", verb]]

    def remote_head(self, name, branch):
        out = sh(["git", "ls-remote", self.origin[name], "refs/heads/%s" % branch], self.tmp).split()
        return out[0] if out else None

    def url(self, name):
        return "https://github.com/acme/%s/pull/7" % name


class TwoRepoPrTest(PrCase):
    def setUp(self):
        super().setUp()
        self.job = self.jira_job()
        self.before = self.to_pr(self.job)

    def test_each_repository_gets_its_pull_request_into_its_target(self):
        res = self.ok("pr", self.job, "--target", "be=main", "--target", "fe=develop")
        raw = self.raw(self.job)
        self.assertEqual(raw["stage"], "handed-off")
        for n, target in (("be", "main"), ("fe", "develop")):
            e = raw["repos"][n]
            self.assertEqual(e["target_branch"], target)
            self.assertEqual(e["pr_url"], self.url(n))
            self.assertEqual(self.remote_head(n, e["branch"]), e["pushed_head"])
        creates = {os.path.basename(c["cwd"]): c["argv"] for c in self.gh_calls("create")}
        self.assertEqual(sorted(creates), ["be", "fe"])
        self.assertEqual(creates["be"][creates["be"].index("--base") + 1], "main")
        self.assertEqual(creates["fe"][creates["fe"].index("--base") + 1], "develop")
        self.assertEqual([(r["name"], r["target"], r["pushed"], r["pr_url"]) for r in res["repos"]],
                         [("be", "main", True, self.url("be")), ("fe", "develop", True, self.url("fe"))])
        self.assertEqual(res["clone"], "removed")  # every repository was pushed
        self.assertFalse(os.path.exists(self.work(self.job)))

    def test_a_plain_target_is_every_repository_s(self):
        self.ok("pr", self.job, "--target", "develop")
        raw = self.raw(self.job)
        self.assertEqual({n: e["target_branch"] for n, e in raw["repos"].items()}, {"be": "develop", "fe": "develop"})

    def test_a_target_of_a_repository_the_job_does_not_have(self):
        code, res = self.fx("pr", self.job, "--target", "zz=main")
        self.assertEqual(code, 3, res)
        code, res = self.fx("pr", self.job, "--target", "fe=nope")
        self.assertEqual(code, 3, res)
        self.assertIn("fe", res["error"])
        self.assertEqual(self.gh_calls(), [])

    def test_the_pull_requests_link_to_each_other(self):
        res = self.ok("pr", self.job)
        bodies = {}
        for c in self.gh_calls():
            if "body" in c:
                bodies.setdefault(os.path.basename(c["cwd"]), []).append(c["body"])
        last = {n: b[-1] for n, b in bodies.items()}
        self.assertIn(self.url("fe"), last["be"])
        self.assertIn(self.url("be"), last["fe"])
        self.assertIn("1. be: this pull request", last["be"])
        self.assertIn("2. fe: this pull request", last["fe"])
        self.assertIn("## This repository's part\n\nAdd POST /api/reset.", last["be"])
        self.assertIn("## This repository's part\n\nAdd the reset form.", last["fe"])
        for n in ("be", "fe"):
            self.assertIn('POST /api/reset takes {"email": string}', last[n])
            self.assertEqual(slurp(os.path.join(self.factory, "jobs", self.job, "pr-body-%s.md" % n)), last[n])
        edits = [c for c in self.gh_calls("edit") if os.path.basename(c["cwd"]) == "be"]
        self.assertEqual(len(edits), 1, "be's description gets fe's link once fe's pull request is open")
        md = slurp(res["pr_body"])
        self.assertIn("## Repo: be", md)
        self.assertIn("## Repo: fe", md)
        self.assertIn("### Pull requests of this issue", md)

    def test_one_jira_comment_and_one_transition(self):
        self.ok("pr", self.job)
        todo = self.raw(self.job)["jira_todo"][self.before:]
        self.assertEqual(sorted(t["action"] for t in todo), ["comment", "transition"])
        comment = [t for t in todo if t["action"] == "comment"][0]["markdown"]
        self.assertIn("- Pull requests (merge them in this order):", comment)
        self.assertIn("  - be: %s (branch" % self.url("be"), comment)
        self.assertIn("  - fe: %s (branch" % self.url("fe"), comment)

    def test_a_secret_in_the_second_repository_blocks_every_push(self):
        self.stub("gitleaks", LEAKS_IN_FE)
        res = self.ok("pr", self.job)
        self.assertEqual((res["pushed"], res["next"], res["repos"]), (False, "build", ["fe"]))
        self.assertTrue(res["secrets"][0].startswith("fe: config.js:3"), res["secrets"])
        self.assertEqual(res["report"], os.path.join(self.factory, "jobs", self.job, "pr-scan", "fe", "security.md"))
        self.assertTrue(os.path.exists(res["report"]))
        raw = self.raw(self.job)
        self.assertEqual(raw["stage"], "build")
        for n in ("be", "fe"):
            self.assertIsNone(self.remote_head(n, raw["repos"][n]["branch"]), n)
            self.assertNotIn("pushed_head", raw["repos"][n])
        self.assertEqual(raw["repos"]["fe"]["secret_bounces"], 1)
        self.assertNotIn("secret_bounces", raw["repos"]["be"])
        self.assertEqual(self.gh_calls(), [])
        task = self.ok("ctx", self.job, "--agent", "builder")["task"]
        self.assertIn("the commits of fe (see pr-scan/fe/security.md", task)
        self.assertIn("in fe's worktree `git reset --soft %s`" % raw["repos"]["fe"]["secret_reset_to"][:12], task)
        # the builder did not take it out: the second time a human must
        ctx = self.ctx()
        j = ctx.load_exact(self.job)
        j["stage"] = "pr"
        ctx.save(j)
        code, res = self.fx("pr", self.job)
        self.assertEqual(code, 3, res)
        self.assertIn("the branch of fe still has secrets", res["error"])
        self.assertIn("Nothing was pushed anywhere", res["error"])

    def test_a_partial_failure_can_be_run_again(self):
        hook = os.path.join(self.origin["fe"], "hooks", "pre-receive")
        write(hook, "#!/bin/sh\necho no pushes today\nexit 1\n")
        os.chmod(hook, 0o755)
        code, res = self.fx("pr", self.job)
        self.assertNotEqual(code, 0, res)
        raw = self.raw(self.job)
        self.assertEqual(raw["stage"], "pr")
        be = raw["repos"]["be"]
        self.assertEqual(self.remote_head("be", be["branch"]), be["pushed_head"])
        self.assertNotIn("pushed_head", raw["repos"]["fe"])
        self.assertEqual(self.gh_calls("create"), [], "no pull request before every repository is pushed")
        self.assertTrue(os.path.isdir(self.work(self.job, "be")))
        os.remove(hook)
        res = self.ok("pr", self.job)
        self.assertIn("be: already pushed and unchanged: not pushed again", res["notes"])
        raw = self.raw(self.job)
        self.assertEqual(raw["repos"]["be"]["pushed_head"], be["pushed_head"])
        self.assertEqual(sorted(os.path.basename(c["cwd"]) for c in self.gh_calls("create")), ["be", "fe"])
        self.assertEqual(res["clone"], "removed")

    def test_the_clones_stay_while_one_repository_is_not_pushed(self):
        sh(["git", "remote", "remove", "origin"], self.work(self.job, "fe"))
        res = self.ok("pr", self.job)
        self.assertEqual(res["clone"], "kept: not pushed (fe)")
        self.assertIn("fe: no remote 'origin'; branch not pushed", res["notes"])
        self.assertTrue(os.path.isdir(self.work(self.job, "be")))
        self.assertTrue(os.path.isdir(self.work(self.job, "fe")))
        raw = self.raw(self.job)
        self.assertEqual(raw["repos"]["be"]["pr_url"], self.url("be"))
        self.assertIsNone(raw["repos"]["fe"].get("pr_url"))

    def test_a_repository_with_nothing_of_its_own_gets_no_pull_request(self):
        ctx = self.ctx()
        j = ctx.load_exact(self.job)
        fe = j["repos"]["fe"]["worktree"]
        sh(["git", "reset", "-q", "--hard", "HEAD~1"], fe)
        res = self.ok("pr", self.job)
        self.assertIn("fe: no commits beyond its base, so nothing to push and no pull request", res["notes"])
        self.assertEqual([os.path.basename(c["cwd"]) for c in self.gh_calls("create")], ["be"])
        body = [c for c in self.gh_calls("create")][0]["body"]
        self.assertNotIn("Pull requests of this issue", body)


class OneRepoPrTest(PrCase):
    def test_a_job_of_one_repository_has_one_plain_description(self):
        job = self.jira_job("be")
        self.to_pr(job, ("be",))
        res = self.ok("pr", job, "--target", "main")
        self.assertNotIn("repos", res)
        self.assertEqual(res["pr_url"], self.url("be"))
        md = slurp(os.path.join(self.factory, "jobs", job, "pr-body.md"))
        self.assertNotIn("## Repo:", md)
        self.assertNotIn("Pull requests of this issue", md)
        self.assertEqual(md, self.gh_calls("create")[0]["body"])
        self.assertEqual(self.gh_calls("edit"), [])


# --------------------------------------------------------------------------
# learning: each repository's pull request, comments and human edits, cursors per repository

class LearnCase(PrCase):
    def handed_off(self):
        job = self.jira_job()
        ctx = self.ctx()
        j = ctx.load_exact(job)
        state = {}
        for n in ("be", "fe"):
            wt = j["repos"][n]["worktree"]
            head = self.commit_in(wt, "%s.txt" % n, "work\n", "%s work" % n)
            sh(["git", "push", "-q", "origin", "HEAD:refs/heads/%s" % j["repos"][n]["branch"]], wt)
            j["repos"][n].update(pushed_head=head, pr_url=self.url(n), target_branch="main")
            state[self.url(n)] = {"state": "OPEN", "head": head, "comments": []}
        j["stage"] = "handed-off"
        ctx.save(j)
        self.state = state
        self.save_state()
        return job

    def save_state(self):
        write(self.gh_state, json.dumps(self.state))

    def human_commit(self, job, name):
        other = os.path.join(self.tmp, "human-" + name)
        sh(["git", "clone", "-q", "-b", self.raw(job)["repos"][name]["branch"], self.origin[name], other], self.tmp)
        head = self.commit_in(other, "fix.txt", "fixed\n", "a human fix")
        sh(["git", "push", "-q", "origin", "HEAD"], other)
        return head


class LessonsAcrossReposTest(LearnCase):
    def test_comments_and_edits_of_each_repository(self):
        job = self.handed_off()
        self.state[self.url("be")]["comments"] = [{"id": "c1", "author": {"login": "ann"}, "body": "Name it reset"}]
        human = self.human_commit(job, "fe")
        self.state[self.url("fe")].update(head=human, comments=[{"id": "c2", "author": {"login": "bob"},
                                                                 "body": "Use the shared button"}])
        self.save_state()
        res = self.ok("lessons", "collect")
        self.assertEqual(res["jobs"], [job])
        item = res["items"][0]
        self.assertEqual(item["prs"], {"be": self.url("be"), "fe": self.url("fe")})
        self.assertEqual(sorted((c["repo"], c["id"]) for c in item["pr_comments"]), [("be", "c1"), ("fe", "c2")])
        self.assertEqual([(e["repo"], [c["subject"] for c in e["commits"]]) for e in item["human_edits"]],
                         [("fe", ["a human fix"])])
        self.assertEqual(item["pr_states"], {"be": "open", "fe": "open"})
        self.ok("lessons", "save", "--jobs", job)
        raw = self.raw(job)
        self.assertEqual(raw["repos"]["be"]["learn"]["comments"], ["c1"])
        self.assertEqual(raw["repos"]["fe"]["learn"]["comments"], ["c2"])
        self.assertEqual(raw["repos"]["fe"]["learn"]["edits_seen"], human)
        self.assertEqual(raw["repos"]["be"]["learn"]["edits_seen"], raw["repos"]["be"]["pushed_head"])
        self.assertNotIn("comments", raw["learn"])
        self.assertTrue(raw["learn"]["factory"])
        self.assertEqual(raw["learn"]["edit_lines"], item["human_edits"][0]["lines_changed"])
        self.assertEqual(self.ok("lessons", "collect")["jobs"], [], "nothing new the second time")

    def test_the_job_is_done_when_every_pull_request_is(self):
        job = self.handed_off()
        self.state[self.url("be")]["state"] = "MERGED"
        self.save_state()
        self.ok("lessons", "collect")
        raw = self.raw(job)
        self.assertTrue(raw["repos"]["be"]["learn"]["final"])
        self.assertFalse(raw["repos"]["fe"]["learn"].get("final"))
        self.assertFalse(raw["learn"].get("final"))
        self.state[self.url("fe")]["state"] = "CLOSED"
        self.save_state()
        self.ok("lessons", "collect")
        self.assertTrue(self.raw(job)["learn"]["final"])

    def test_an_older_job_s_cursors_become_its_repository_s(self):
        job = self.handed_off()
        path = os.path.join(self.factory, "jobs", job, "job.json")
        data = load_json(path)
        data["learn"] = {"factory": True, "rework": [1], "comments": ["old"], "edits_seen": "abc",
                         "pending_edits": [["a", "b"]]}
        write(path, json.dumps(data))
        j = self.ctx().load_exact(job)
        self.assertEqual(j["learn"], {"factory": True, "rework": [1]})
        self.assertEqual(j["repos"]["be"]["learn"], {"comments": ["old"], "edits_seen": "abc",
                                                     "pending_edits": [["a", "b"]]})
        # a cursor an older fx left in pending.json is the first repository's
        fx.apply_cursor(self.ctx(), job, {"comments": ["c9"], "final": True})
        raw = self.raw(job)
        self.assertEqual(raw["repos"]["be"]["learn"]["comments"], ["old", "c9"])
        self.assertTrue(raw["learn"]["final"])


class MergedPerRepoTest(LearnCase):
    def test_merged_only_when_every_pull_request_is(self):
        job = self.handed_off()
        write(os.path.join(self.factory, "jobs", job, "logic", "auth", "reset.md"), "# Reset\n")
        self.state[self.url("be")]["state"] = "MERGED"
        self.save_state()
        ctx = self.ctx()
        self.assertEqual(fx.job_merged_repos(ctx, ctx.load_exact(job)), {"be": "merged", "fe": "open"})
        self.assertEqual(fx.job_merged(ctx, ctx.load_exact(job)), "open")
        res = self.ok("logic", "pending")
        self.assertEqual(res["ready"], [])
        self.assertEqual(res["waiting"][0]["repos"]["be"]["state"], "merged")
        self.assertEqual(res["waiting"][0]["repos"]["fe"]["state"], "open")
        code, res = self.fx("logic", "apply", "--job", job)
        self.assertEqual(code, 3, res)
        self.assertIn("of fe of %s is not merged yet" % job, res["error"])
        self.state[self.url("fe")]["state"] = "MERGED"
        self.save_state()
        self.assertEqual(fx.job_merged(ctx, ctx.load_exact(job)), "merged")
        self.assertEqual([r["job"] for r in self.ok("logic", "pending")["ready"]], [job])

    def test_one_merged_and_one_closed_is_partial_and_the_user_decides(self):
        job = self.handed_off()
        write(os.path.join(self.factory, "jobs", job, "logic", "auth", "reset.md"), LOGIC_FILE)
        self.state[self.url("be")]["state"] = "MERGED"
        self.state[self.url("fe")]["state"] = "CLOSED"
        self.save_state()
        ctx = self.ctx()
        self.assertEqual(fx.job_merged(ctx, ctx.load_exact(job)), "partial")
        res = self.ok("logic", "pending")
        self.assertEqual(res["ready"], [])
        row = res["waiting"][0]
        self.assertEqual((row["job"], row["state"]), (job, "partial"))
        self.assertEqual({n: r["state"] for n, r in row["repos"].items()}, {"be": "merged", "fe": "closed"})
        self.assertEqual(row["files"], ["auth/reset.md"])
        self.assertNotIn("logic", self.raw(job).get("learn") or {}, "a listing never discards the staged logic")
        self.assertEqual(self.ok("logic", "pending")["waiting"][0]["state"], "partial", "still there the next time")
        self.assertTrue(os.path.exists(os.path.join(self.factory, "jobs", job, "logic", "auth", "reset.md")))
        res = self.ok("logic", "apply", "--job", job)  # the user chose to apply it: no --force needed
        self.assertEqual(res["applied"], ["auth/reset.md"])
        self.assertTrue(os.path.exists(os.path.join(self.factory, "logic", "auth", "reset.md")))

    def test_every_one_closed_is_listed_not_discarded(self):
        job = self.handed_off()
        write(os.path.join(self.factory, "jobs", job, "logic", "auth", "reset.md"), "# Reset\n")
        for n in ("be", "fe"):
            self.state[self.url(n)]["state"] = "CLOSED"
        self.save_state()
        res = self.ok("logic", "pending")
        self.assertEqual(res["waiting"][0]["state"], "closed")
        self.assertNotIn("logic", self.raw(job).get("learn") or {})
        code, res = self.fx("logic", "apply", "--job", job)
        self.assertEqual(code, 3, res)

    def test_git_has_asks_the_named_repository(self):
        self.handed_off()
        ctx = self.ctx()
        self.assertFalse(fx.git_has(ctx, "only-on-develop.txt"))  # on develop only, in neither base branch
        self.assertTrue(fx.git_has(ctx, "be:README.md"))
        self.assertTrue(fx.git_has(ctx, "fe:README.md:1-2"))
        self.assertFalse(fx.git_has(ctx, "be:missing/thing.py"))
        self.assertFalse(fx.git_has(ctx, "missing/thing.py"))


# --------------------------------------------------------------------------
# a rework after one repository's pull request was merged

class ReworkAfterMergeTest(LearnCase):
    def merge_be(self, job, delete=True):
        """be's pull request is merged into main, and (delete) its branch deleted, as hosts do."""
        self.state[self.url("be")]["state"] = "MERGED"
        self.save_state()
        branch = self.raw(job)["repos"]["be"]["branch"]
        tmp = os.path.join(self.tmp, "merge-be")
        sh(["git", "clone", "-q", self.origin["be"], tmp], self.tmp)
        sh(["git", "merge", "-q", "--no-edit", "origin/%s" % branch], tmp)
        sh(["git", "push", "-q", "origin", "HEAD:main"], tmp)
        if delete:
            sh(["git", "push", "-q", "origin", "--delete", branch], tmp)

    def rework(self, job):
        self.ok("cleanup", job)
        return self.ok("rework", job, "the form needs a second field")

    def test_the_merged_repository_leaves_the_rework_and_the_gate(self):
        job = self.handed_off()
        pr_be = self.raw(job)["repos"]["be"]["pr_url"]
        self.merge_be(job)
        res = self.rework(job)
        self.assertIn("be: its pull request is merged, so this rework does not change it", res["notes"])
        raw = self.raw(job)
        be = raw["repos"]["be"]
        self.assertEqual(be["role"], "run")
        self.assertTrue(be["merged"])
        self.assertEqual(be["pr_url"], pr_be, "its pull request stays recorded for learning")
        self.assertIsNone(be["worktree"])  # no app needs it
        self.assertFalse(os.path.exists(self.work(job, "be")))
        self.assertEqual(fx.job_names(raw, "change"), ["fe"])
        self.assertTrue(os.path.isdir(self.work(job, "fe")))
        # the gate checks fe only
        self.commit_in(self.work(job, "fe"), "second.txt", "field\n")
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["risk"] = "low"
        ctx.save(j)
        code, res = self.fx("gate", job)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["gate"], "PASS", slurp(res["report"]))
        # learning still reads be's pull request
        ctx = self.ctx()
        item, cur, _ = fx.collect_job(ctx, ctx.load_exact(job), {})
        self.assertEqual(item["prs"]["be"], pr_be)
        self.assertEqual(item["pr_states"]["be"], "merged")
        self.assertIn("be", cur["repos"])
        self.assertEqual(fx.job_merged_repos(ctx, ctx.load_exact(job)), {"be": "merged", "fe": "open"})

    def test_a_merged_branch_that_is_still_there(self):
        job = self.handed_off()
        self.merge_be(job, delete=False)
        self.rework(job)
        raw = self.raw(job)
        self.assertEqual(raw["repos"]["be"]["role"], "run")
        self.commit_in(self.work(job, "fe"), "second.txt", "field\n")
        code, res = self.fx("gate", job)
        self.assertEqual(res["gate"], "PASS", slurp(res["report"]))
        self.assertNotIn("no commits beyond", slurp(res["report"]))
        # fx pr pushes fe only
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j.update(stage="pr", decision="APPROVE")
        ctx.save(j)
        res = self.ok("pr", job)
        self.assertEqual({os.path.basename(c["cwd"]) for c in self.gh_calls() if c["argv"][:2] in (
            ["pr", "create"], ["pr", "edit"], ["pr", "ready"])}, {"fe"})
        raw = self.raw(job)
        self.assertEqual(self.remote_head("fe", raw["repos"]["fe"]["branch"]), raw["repos"]["fe"]["pushed_head"])

    def test_every_pull_request_merged_means_a_new_job(self):
        job = self.handed_off()
        self.merge_be(job)
        self.state[self.url("fe")]["state"] = "MERGED"
        self.save_state()
        self.ok("cleanup", job)
        code, res = self.fx("rework", job, "more")
        self.assertEqual(code, 3, res)
        self.assertIn("Start a new job", res["error"])
        self.assertEqual(self.raw(job)["stage"], "handed-off")


class ReworkAfterMergeRunTest(ReworkAfterMergeTest):
    """fe's app needs be's: the merged be comes back as a checkout of its base, to run."""
    extra = {"be": {"app": {"up": "true"}}, "fe": {"app": {"up": "true", "needs": ["be"]}}}

    def test_the_merged_repository_leaves_the_rework_and_the_gate(self):
        job = self.handed_off()
        self.merge_be(job)
        res = self.rework(job)
        self.assertTrue(any("it runs from its base main" in n for n in res["notes"]), res["notes"])
        be = self.raw(job)["repos"]["be"]
        self.assertEqual((be["role"], be["worktree"]), ("run", self.work(job, "be")))
        wt = self.work(job, "be")
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt).strip(), "HEAD")
        self.assertTrue(os.path.exists(os.path.join(wt, "be.txt")), "the base has the merged work")

    test_a_merged_branch_that_is_still_there = None  # the base class's: its gate would start these apps (`true`)


# --------------------------------------------------------------------------
# pr.create is off unless factory.json turns it on

class PrCreateDefaultTest(PrCase):
    top = {"pr": {"provider": "github", "labels": []},
           "jira": {"base_url": JIRA, "transitions": {"start": "In Progress", "review": "In Review"}}}

    def test_no_pull_request_without_pr_create(self):
        job = self.jira_job("be")
        self.to_pr(job, ("be",))
        res = self.ok("pr", job)
        self.assertEqual(self.gh_calls("create"), [])
        self.assertIsNone(res["pr_url"])
        self.assertIn("opened by hand (pr.create is off)", res["pr_hint"])
        e = self.raw(job)["repos"]["be"]
        self.assertEqual(self.remote_head("be", e["branch"]), e["pushed_head"])


# --------------------------------------------------------------------------
# fx pr pushes to factory.json's URL, runs no hook of the clone, and names the repository to gh and glab

class PushTargetTest(PrCase):
    def test_the_url_of_factory_json_not_the_clone_s_origin(self):
        job = self.jira_job("be")
        self.to_pr(job, ("be",))
        wt = self.work(job, "be")
        decoy = os.path.join(self.tmp, "decoy.git")
        sh(["git", "init", "-q", "--bare", decoy], self.tmp)
        sh(["git", "remote", "set-url", "origin", decoy], wt)
        marker = os.path.join(self.tmp, "hook-ran")
        for hook in ("pre-push", "pre-merge-commit", "post-merge", "pre-commit", "post-commit"):
            path = os.path.join(wt, ".git", "hooks", hook)
            write(path, "#!/bin/sh\necho %s >> %s\nexit 1\n" % (hook, marker))
            os.chmod(path, 0o755)
        # the target moved on: fx pr merges it (a real merge commit) before it pushes
        tmp = os.path.join(self.tmp, "moves-main")
        sh(["git", "clone", "-q", self.origin["be"], tmp], self.tmp)
        self.commit_in(tmp, "other.txt", "other\n", "someone else's work")
        sh(["git", "push", "-q", "origin", "HEAD:main"], tmp)
        res = self.ok("pr", job)
        self.assertFalse(os.path.exists(marker), slurp(marker) if os.path.exists(marker) else "")
        self.assertFalse(res["draft"], res["notes"])
        e = self.raw(job)["repos"]["be"]
        self.assertEqual(self.remote_head("be", e["branch"]), e["pushed_head"])
        self.assertEqual(sh(["git", "for-each-ref", "refs/heads"], decoy).strip(), "", "nothing went to the decoy")

    def test_gh_and_glab_are_told_the_repository(self):
        self.assertEqual(fx.host_repo_flag("github", "git@github.com:acme/shop-api.git"), ["--repo", "acme/shop-api"])
        self.assertEqual(fx.host_repo_flag("github", "https://github.com/acme/shop-api"), ["--repo", "acme/shop-api"])
        self.assertEqual(fx.host_repo_flag("github", "https://ghe.example.com/acme/api.git"),
                         ["--repo", "ghe.example.com/acme/api"])
        self.assertEqual(fx.host_repo_flag("gitlab", "git@gitlab.com:team/sub/web.git"), ["-R", "team/sub/web"])
        self.assertEqual(fx.host_repo_flag("gitlab", "https://git.example.com/team/web.git"),
                         ["-R", "https://git.example.com/team/web"])
        self.assertEqual(fx.host_repo_flag("github", self.origin["be"]), [])
        job = self.jira_job("be")
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j["repos"]["be"]["target_branch"] = "main"
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/acme/shop-api/pull/9\n", "")
        with mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx, "remote_provider", return_value=("github", "git@github.com:acme/shop-api.git")), \
                mock.patch.object(fx.shutil, "which", return_value="/usr/bin/gh"):
            ctx.cfg["pr"] = {"create": True}
            url, _ = fx.pr_open(ctx, j, "be", "t", "b", os.path.join(self.tmp, "body.md"), ["l"], False, False, [], "")
        self.assertEqual(url, "https://github.com/acme/shop-api/pull/9")
        gh = [c for c in calls if c[0] == "gh"]
        self.assertTrue(gh)
        for c in gh:
            self.assertEqual(c[c.index("--repo") + 1], "acme/shop-api", c)


if __name__ == "__main__":
    unittest.main()
